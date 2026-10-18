from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from scripts.export_on_demand import export_on_demand
from scripts.process_existing_raw import ProcessExistingRawDeps, process_existing_raw
from scripts.run_daily_export import DailyExportDeps, run_daily_export
from wechat_diary_core.archiving import archive
from wechat_diary_core.chat_flow import render_message_content
from wechat_diary_core.config import ImageLabelConfig, load_config
from wechat_diary_core.preprocessing.cleaner import prepare_chat_messages
from wechat_diary_core.preprocessing.image_vision import annotate_vision_descriptions, annotate_moment_vision
from wechat_diary_core.preprocessing.vision_service import VisionService, LABEL_MODEL
from wechat_diary_core.preprocessing.vision_store import VisionStore, atomic_json
from wechat_diary_core.preprocessing.vision_transport import (
    CheapApiTransport, CodexTransport, VisionResponse, MAX_IMAGE_BYTES,
)
from wechat_diary_core.preprocessing.vision_records import VisionError, ProviderUnavailable, parse_records
from wechat_diary_core.preprocessing.vision_prompts import OUTPUT_SCHEMA, LABEL_SCHEMA, LABEL_PROMPT
from test_vision_contract import record, response


def coarse(slot=1, **kwargs):
    return dict(dict(slot=slot, orientation="竖", anchor="synthetic", kind="照片", person="无", text="少",
                     topic="生活日常", ip="无", nature="人物与生活记录", sensitive="无"), **kwargs)


class FakeTransport:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def request(self, paths, context, system, schema=None, *, max_tokens=None):
        self.calls.append(dict(paths=list(paths), context=context, system=system, schema=schema,
                               max_tokens=max_tokens, bytes=[p.read_bytes() for p in paths]))
        if self.handler:
            return self.handler(paths, context, system, schema, len(self.calls))
        return VisionResponse(response(len(paths)))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = replace(load_config(self.root / "missing.toml").preprocessing.image_vision,
                                enabled=True, backend="codex", model="gpt-6.1-sol")
        self.no_labels = ImageLabelConfig(enabled=False)

    def image(self, name, color=(70, 40, 10), size=(600, 900)):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, color).save(path)
        return path

    def messages(self, count=2, *, same_sender=True):
        messages = []
        for i in range(count):
            name = f"media/images/{i}.png"
            self.image(name, (10 + i * 20, 30, 60))
            messages.append(dict(type="图片消息", content=name, source=name, senderUsername="alice" if same_sender else f"peer_{i}",
                formattedTime=f"2026-10-18 10:{i:02}:00", isSend=0, platformMessageId=f"image-{i}"))
        return messages

    def annotate(self, messages, fake, **kwargs):
        return annotate_vision_descriptions(messages, self.root, kwargs.pop("settings", self.settings), self.root / "archive",
            lambda m, base: [base / str(m.get("content")), base / str(m.get("source"))]
                if m.get("type") == "图片消息" else [], describer=fake,
            label_settings=kwargs.pop("label_settings", self.no_labels), **kwargs)

    def successes(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in (self.root / "archive/_image-descriptions").glob("[0-9a-f][0-9a-f]/*/*.json")]

    def test_full_group_resend_only_writes_missing_and_keeps_context_and_hit_bytes(self):
        messages = self.messages()
        def first(paths, context, system, schema, call):
            rows = [record(1), record(2, gist="")]
            return VisionResponse(json.dumps(dict(group_summary="", images=rows), ensure_ascii=False))
        a = self.annotate(deepcopy(messages), FakeTransport(first))
        self.assertIn("image_vision", a[0])
        self.assertNotIn("image_vision", a[1])
        files = list((self.root / "archive/_image-descriptions").glob("[0-9a-f][0-9a-f]/*/*.json"))
        self.assertEqual(len(files), 1)
        old = files[0].read_bytes()
        fake = FakeTransport()
        b = self.annotate(deepcopy(messages), fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(fake.calls[0]["paths"]), 2)
        self.assertEqual(files[0].read_bytes(), old)
        self.assertEqual(b[1]["image_context_sha256"], a[0]["image_context_sha256"])
        self.assertEqual(len(self.successes()), 2)
        self.assertEqual(self.successes()[0]["unit"]["group_size"], 2)
        self.annotate(deepcopy(messages), FakeTransport(lambda *args: self.fail("strong hit must make zero requests")))

    def test_weak_hit_no_request_model_and_prompt_versions_do_not_cross(self):
        messages = self.messages(1)
        self.annotate(deepcopy(messages), FakeTransport())
        new = [dict(type="文本消息", content="different context", senderUsername="bob", formattedTime="2026-10-18 09:59:00")] + deepcopy(messages)
        result = self.annotate(new, FakeTransport(lambda *args: self.fail("weak hit must make zero requests")))
        self.assertTrue(result[1]["weak_hit"])
        self.assertEqual(result[1]["image_vision_pending"]["reason"], "weak_hit")
        store = VisionStore(self.root / "archive/_image-descriptions")
        exact = deepcopy(self.successes()[0])
        exact["context_sha256"] = result[1]["image_context_sha256"]
        store.put(exact)
        self.annotate(result, FakeTransport(lambda *args: self.fail("new strong cache must be reused")))
        self.assertNotIn("weak_hit", result[1])
        self.assertNotIn("image_vision_pending", result[1])
        fake = FakeTransport()
        self.annotate(deepcopy(messages), fake, settings=replace(self.settings, model="gpt-6-luna", effort="low"))
        self.assertEqual(len(fake.calls), 1)
        store = VisionStore(self.root / "archive/_image-descriptions")
        entry = self.successes()[0]
        self.assertEqual(store.lookup(entry["image_sha256"], entry["context_sha256"], entry["model"], "other-version"), (None, False))

    def test_multiple_groups_invalid_pack_splits_once_then_single_groups_retry_once(self):
        messages = self.messages(3, same_sender=False)
        fake = FakeTransport(lambda paths, *args: VisionResponse(response(1)) if len(paths) > 1 else VisionResponse(response(1)))
        self.annotate(messages, fake)
        self.assertEqual([len(c["paths"]) for c in fake.calls], [3, 1, 1, 1])
        self.assertEqual(len(self.successes()), 3)
        bad = FakeTransport(lambda *args: VisionResponse("{'invalid':true}"))
        separate = replace(self.settings, model="different")
        result = self.annotate(self.messages(1), bad, settings=separate)
        self.assertEqual(len(bad.calls), 2)
        self.assertNotIn("image_vision", result[0])
        self.assertEqual(len(self.successes()), 3)

    def test_untested_codex_model_defaults_to_one_group_per_pack(self):
        messages = self.messages(2, same_sender=False)
        luna = FakeTransport()
        self.annotate(deepcopy(messages), luna, settings=replace(self.settings, model="gpt-6-luna", effort="low"))
        self.assertEqual([len(c["paths"]) for c in luna.calls], [1, 1])
        sol = FakeTransport()
        self.annotate(deepcopy(messages), sol)
        self.assertEqual([len(c["paths"]) for c in sol.calls], [2])

    def test_shift_rejected_before_gist_rewrite_and_bad_pack_not_cached(self):
        messages = self.messages(2, same_sender=False)
        def label(paths, *args):
            return VisionResponse(json.dumps(dict(items=[coarse(1, kind="照片"), coarse(2, kind="文档图")])))
        labeler = FakeTransport(label)
        def formal(paths, context, system, schema, call):
            if len(paths) == 2:
                return VisionResponse(json.dumps(dict(group_summary="", images=[
                    record(1, kind="文档图", gist="发图人回应"), record(2, kind="照片")]), ensure_ascii=False))
            return VisionResponse(response(1))
        fake = FakeTransport(formal)
        self.annotate(messages, fake, label_settings=ImageLabelConfig(), labeler=labeler)
        self.assertEqual([len(c["paths"]) for c in fake.calls], [2, 1, 1])
        self.assertTrue(all(x["unit"]["pack_size"] == 1 for x in self.successes()))
        events = (self.root / "archive/_image-descriptions/_runs/events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"pack_shift_suspect"', events)
        self.assertNotIn("发图人回应", events)
        self.assertNotIn("media/", events)

    def test_sensitive_removed_before_grouping_no_bytes_or_label_in_sibling_context_unlock(self):
        messages = self.messages()
        secret_bytes = (self.root / messages[0]["content"]).read_bytes()
        labeler = FakeTransport(lambda paths, *args: VisionResponse(json.dumps(dict(items=[coarse(1, sensitive="证件"), coarse(2)]))))
        fake = FakeTransport()
        result = self.annotate(deepcopy(messages), fake, label_settings=ImageLabelConfig(), labeler=labeler)
        self.assertEqual(result[0]["image_vision_inline"], "照片｜含证件，未转写")
        self.assertEqual(len(fake.calls), 1)
        self.assertNotIn(secret_bytes, fake.calls[0]["bytes"])
        self.assertIn("[图片，不在本组]", fake.calls[0]["context"])
        self.assertNotIn("证件", fake.calls[0]["context"])
        self.assertNotIn("本组图2", fake.calls[0]["context"])
        # The label request has thumbnails and only the fixed batch count, no chat.
        self.assertEqual(labeler.calls[0]["context"], "本批共 2 张。")
        self.assertEqual(labeler.calls[0]["system"], LABEL_PROMPT)
        self.assertNotIn("alice", labeler.calls[0]["context"])
        unlock = replace(self.settings, allow_image_sha256=[hashlib.sha256(secret_bytes).hexdigest()])
        rerun = FakeTransport()
        result = self.annotate(deepcopy(messages), rerun, settings=unlock, label_settings=ImageLabelConfig(), labeler=labeler)
        self.assertEqual(len(labeler.calls), 1)
        self.assertEqual(len(rerun.calls[0]["paths"]), 2)
        self.assertIn("image_vision", result[0])
        self.assertTrue(result[1]["weak_hit"])

    def test_all_sensitive_and_skip_or_disabled_are_zero_formal_or_all_calls(self):
        messages = self.messages(1)
        labeler = FakeTransport(lambda *args: VisionResponse(json.dumps(dict(items=[coarse(1, sensitive="账号密码")]))))
        formal = FakeTransport(lambda *args: self.fail("sensitive must not be sent"))
        result = self.annotate(messages, formal, settings=replace(self.settings, allow_usernames=["", " "]),
                               label_settings=ImageLabelConfig(), labeler=labeler)
        self.assertEqual(len(labeler.calls), 1)
        self.assertEqual(len(self.successes()), 0)
        self.assertIn("含账号密码，未转写", render_message_content(result[0]))
        for settings, session in ((replace(self.settings, enabled=False), {}),
                                  (replace(self.settings, skip_usernames=["excluded"]), dict(wxid="excluded"))):
            self.annotate(self.messages(1), formal, settings=settings, session=session,
                          label_settings=ImageLabelConfig(), labeler=FakeTransport(lambda *args: self.fail("no label call")))
        self.assertEqual(formal.calls, [])

    def test_label_failure_nonblocking_and_batch_validation_rejects_duplicate_slots(self):
        messages = self.messages(2)
        labeler = FakeTransport(lambda *args: VisionResponse(json.dumps(dict(items=[coarse(1), coarse(1)]))))
        formal = FakeTransport()
        self.annotate(messages, formal, label_settings=ImageLabelConfig(), labeler=labeler)
        self.assertEqual(len(formal.calls), 1)
        self.assertTrue(all(x["label"] is None for x in self.successes()))
        self.assertEqual(list((self.root / "archive/_image-descriptions/_labels").rglob("*.json")), [])

    def test_label_batches_and_cache_are_once_per_content_no_context(self):
        messages = self.messages(5)
        def label(paths, context, system, schema, call):
            self.assertEqual(schema, LABEL_SCHEMA)
            for p in paths:
                with Image.open(p) as thumb:
                    self.assertLessEqual(max(thumb.size), 512)
            return VisionResponse(json.dumps(dict(items=[coarse(i + 1) for i in range(len(paths))])))
        labeler = FakeTransport(label)
        self.annotate(deepcopy(messages), FakeTransport(), label_settings=ImageLabelConfig(batch_size=2), labeler=labeler)
        self.assertEqual([len(x["paths"]) for x in labeler.calls], [2, 2, 1])
        self.annotate(deepcopy(messages), FakeTransport(), label_settings=ImageLabelConfig(batch_size=2), labeler=labeler)
        self.assertEqual(len(labeler.calls), 3)

    def test_timeout_length_bad_json_fall_back_without_caching_and_provider_stops(self):
        messages = self.messages(2, same_sender=False)
        for mode in ("timeout", "length", "json"):
            def handler(*args):
                if mode == "timeout":
                    raise subprocess.TimeoutExpired(["fake"], 1)
                return VisionResponse(response(2), "length") if mode == "length" else VisionResponse("not JSON")
            fake = FakeTransport(handler)
            result = self.annotate(deepcopy(messages), fake, settings=replace(self.settings, pack_max=1))
            self.assertEqual(len(fake.calls), 2 if mode == "length" else 4)
            self.assertTrue(all("image_vision" not in m for m in result))
        fake = FakeTransport(lambda *args: (_ for _ in ()).throw(ProviderUnavailable("auth")))
        with self.assertLogs("wechat_diary_core.preprocessing.vision_service", level="WARNING") as logs:
            self.annotate(deepcopy(messages), fake, settings=replace(self.settings, pack_max=1))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(len(self.successes()), 0)

    def test_qwen_budget_retry_is_shared_not_nested(self):
        fake = FakeTransport(lambda *args: VisionResponse("", "length"))
        self.annotate(self.messages(1), fake, settings=replace(self.settings, backend="cheap-api", provider="jisuan", model="qwen38", max_tokens=8000))
        self.assertEqual([x["max_tokens"] for x in fake.calls], [8000, 16000])
        malformed = FakeTransport(lambda paths, context, system, schema, call:
            VisionResponse("bad JSON") if call == 1 else VisionResponse(response(len(paths))))
        self.annotate(self.messages(1), malformed, settings=replace(self.settings,
            backend="cheap-api", provider="jisuan", model="qwen38", max_tokens=8000))
        self.assertEqual([x["max_tokens"] for x in malformed.calls], [8000, 8000])

    def test_single_flight_with_concurrent_consumers(self):
        messages = self.messages(1)
        fake = FakeTransport()
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: self.annotate(deepcopy(messages), fake), range(4)))
        self.assertEqual(len(fake.calls), 1)
        self.assertTrue(all(x[0]["image_vision_inline"] for x in values))

    def test_gist_rewrite_has_no_images_and_raw_output_retains_original_detail(self):
        def formal(paths, context, system, schema, call):
            if not paths:
                return VisionResponse("照片展示一组周边")
            return VisionResponse(response(1, gist="发图人回应", detail="属性描述。" * 400))
        fake = FakeTransport(formal)
        self.annotate(self.messages(1), fake)
        self.assertEqual([len(c["paths"]) for c in fake.calls], [1, 0])
        entry = self.successes()[0]
        self.assertEqual(entry["record"]["gist"], "照片展示一组周边")
        self.assertTrue(entry["checks"]["gist_rewritten"])
        self.assertIn("发图人回应", entry["raw_output"])
        self.assertGreater(entry["checks"]["detail_cut_chars"], 0)
        self.assertEqual(json.loads(entry["raw_output"])["images"][0]["detail"], "属性描述。" * 400)

    def test_failed_split_child_stops_other_children_on_provider_unavailable(self):
        def formal(paths, context, system, schema, call):
            if len(paths) > 1:
                return VisionResponse("invalid json")
            raise ProviderUnavailable("auth")
        fake = FakeTransport(formal)
        self.annotate(self.messages(3, same_sender=False), fake)
        self.assertEqual([len(x["paths"]) for x in fake.calls], [3, 1])
        self.assertEqual(self.successes(), [])

    def test_non_image_audio_video_and_oversize_excluded_but_static_gif_transcribed(self):
        messages = self.messages(1)
        for i, name in enumerate(("audio.wav", "movie.mp4", "fake.png", "large.bmp"), 1):
            path = self.root / name
            if name == "large.bmp":
                Image.new("RGB", (1800, 1800)).save(path)
                self.assertGreater(path.stat().st_size, MAX_IMAGE_BYTES)
            else:
                path.write_bytes(b"not an image")
            messages.append(dict(type="图片消息", content=name, source=name, senderUsername=f"peer{i}", formattedTime=f"2026-10-18 10:{i:02}:00"))
        gif = self.image("valid.gif", size=(100, 100))
        messages.append(dict(type="图片消息", content="valid.gif", source="valid.gif", senderUsername="last", formattedTime="2026-10-18 10:08:00"))
        fake = FakeTransport()
        self.annotate(messages, fake)
        self.assertEqual(sum(len(x["paths"]) for x in fake.calls), 2)
        self.assertTrue(any(gif in x["paths"] for x in fake.calls))

    def test_local_signals_do_not_deduplicate_near_images_or_lose_small_images(self):
        messages = self.messages(2)
        self.image(messages[1]["content"], color=(20, 20, 20), size=(100, 200))
        with patch("imagehash.phash", return_value="8000000000000000"):
            self.annotate(messages, FakeTransport())
        self.assertTrue(messages[1]["image_signals"]["small_image"])
        self.assertIsInstance(messages[1]["image_signals"]["phash"], str)
        self.assertNotEqual(messages[0]["image_signals"]["image_sha256"], messages[1]["image_signals"]["image_sha256"])
        self.assertEqual(len(self.successes()), 2)
        self.assertEqual(messages[0]["image_signals"]["phash_cluster"], messages[1]["image_signals"]["phash_cluster"])
        self.assertEqual(messages[1]["image_signals"]["cluster_rank"], 2)

    def test_broken_phash_binary_and_absent_pillow_do_not_block_formal_calls(self):
        messages = self.messages(1)
        with patch("imagehash.phash", side_effect=ValueError("binary incompatible")):
            self.annotate(deepcopy(messages), FakeTransport())
        self.assertEqual(len(self.successes()), 1)
        import builtins
        original_import = builtins.__import__
        def without_pillow(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError("missing")
            return original_import(name, *args, **kwargs)
        fake = FakeTransport()
        with patch("builtins.__import__", side_effect=without_pillow):
            self.annotate(deepcopy(messages), fake, settings=replace(self.settings, model="header-fallback"))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(self.successes()), 2)

    def test_multi_image_moments_share_body_videos_and_comments_excluded_paths_readable(self):
        self.image("media/a.png")
        self.image("media/b.gif", color=(20, 90, 10))
        (self.root / "media/v.mp4").write_bytes(b"video")
        posts = [dict(username="owner", contentDesc="太阳落山", createTime=1786579200,
                      comments=[dict(nickname="hidden", content="not sent")], media=[dict(localPath=x) for x in
                      ("media/a.png", "media/b.gif", "media/v.mp4")])]
        fake = FakeTransport()
        annotate_moment_vision(posts, self.root, self.settings, self.root / "archive", describer=fake, label_settings=self.no_labels)
        self.assertEqual(len(fake.calls[0]["paths"]), 2)
        self.assertIn("太阳落山", fake.calls[0]["context"])
        self.assertNotIn("not sent", fake.calls[0]["context"])
        self.assertEqual([bool(m.get("image_vision")) for m in posts[0]["media"]], [True, True, False])

    def test_moment_labels_batch_across_posts_without_merging_context_groups(self):
        posts = []
        for i in range(3):
            media = []
            for j in range(2):
                name = f"media/post-{i}-{j}.png"
                self.image(name, color=(i * 30, j * 30, 50), size=(100, 150))
                media.append(dict(localPath=name))
            posts.append(dict(username="owner", createTime=1786579200 + i * 60,
                              contentDesc=f"post {i}", media=media))
        labeler = FakeTransport(lambda paths, *args: VisionResponse(json.dumps(dict(items=[coarse(i + 1) for i in range(len(paths))]))))
        formal = FakeTransport()
        annotate_moment_vision(posts, self.root, self.settings, self.root / "archive", describer=formal,
                              label_settings=ImageLabelConfig(batch_size=4), labeler=labeler)
        self.assertEqual([len(c["paths"]) for c in labeler.calls], [4, 2])
        self.assertEqual([len(c["paths"]) for c in formal.calls], [6])
        self.assertIn("【语境·组3】本条动态正文", formal.calls[0]["context"])


class TransportTests(unittest.TestCase):
    def test_model_alias_resolved_from_public_catalog_without_provider_in_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "scripts/fake.py"
            script.parent.mkdir()
            catalog = root / "config/cheap_api.json"
            catalog.parent.mkdir()
            providers = {name: {"models": {"pro": {"id": "concrete-vision-model"}}}
                         for name in ("provider_a", "provider_b")}
            catalog.write_text(json.dumps({"providers": providers}), encoding="utf-8")
            settings = load_config(root / "missing.toml").preprocessing.image_vision
            identities = [CheapApiTransport(replace(settings, provider=p), api_script=script).model_identity()
                          for p in ("provider_a", "provider_b")]
            self.assertEqual(identities, ["concrete-vision-model"] * 2)
            self.assertNotEqual(CheapApiTransport(replace(settings, provider="provider_a", model="other-model"), api_script=script).model_identity(), identities[0])
            catalog.write_text("{}", encoding="utf-8")
            with self.assertRaises(ProviderUnavailable):
                CheapApiTransport(settings, api_script=script).model_identity()

    def test_cheap_api_four_states_and_image_order_with_forged_process_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "first.png", root / "second.png"]
            for p in paths:
                Image.new("RGB", (50, 100)).save(p)
            settings = load_config(root / "missing.toml").preprocessing.image_vision
            for mode in ("good", "empty", "length", "json", "timeout", "nonzero"):
                calls = []
                def runner(command, stdin, timeout):
                    calls.append(command)
                    if mode == "timeout":
                        raise subprocess.TimeoutExpired(command, timeout)
                    content = "" if mode == "empty" else "{'bad':true}" if mode == "json" else response(2)
                    envelope = dict(choices=[dict(finish_reason="length" if mode == "length" else "stop", message=dict(content=content))])
                    return subprocess.CompletedProcess(command, 1 if mode == "nonzero" else 0, json.dumps(envelope).encode(), b"sensitive stderr")
                transport = CheapApiTransport(settings, runner=runner, api_script=root / "fake.py")
                if mode == "good":
                    result = transport.request(paths, "context", "system", OUTPUT_SCHEMA)
                    self.assertEqual(len(parse_records(result.content, 2)), 2)
                    command = calls[0]
                    self.assertEqual([command[i + 1] for i, x in enumerate(command) if x == "--image"], list(map(str, paths)))
                else:
                    with self.subTest(mode=mode), self.assertRaises((VisionError, subprocess.TimeoutExpired)):
                        result = transport.request(paths, "context", "system", OUTPUT_SCHEMA)
                        parse_records(result.content, 2)

    def test_codex_four_states_N_mismatch_new_output_and_tool_event_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / "first.png", Path(directory) / "second.png"]
            for path in paths:
                Image.new("RGB", (50, 100)).save(path)
            settings = replace(load_config(Path(directory) / "missing.toml").preprocessing.image_vision,
                               backend="codex", model="gpt-6.1-sol", codex_command="fake-codex.exe")
            output_paths = []
            for mode in ("good", "missing", "empty", "json", "wrong_N", "timeout", "tool", "bad_event", "nonzero"):
                def runner(command, stdin, timeout):
                    last = Path(command[command.index("-o") + 1])
                    output_paths.append(last)
                    if mode == "timeout":
                        raise subprocess.TimeoutExpired(command, timeout)
                    if mode != "missing":
                        last.write_text("" if mode == "empty" else "bad JSON" if mode == "json" else response(1 if mode == "wrong_N" else 2), encoding="utf-8")
                    self.assertIn("features.shell_tool=false", command)
                    self.assertIn('web_search="disabled"', command)
                    self.assertIn("--ignore-user-config", command)
                    self.assertIn("--output-schema", command)
                    stdout = json.dumps(dict(type="item.completed", item=dict(type="command_execution"))).encode() if mode == "tool" else b'{"type":"turn.completed"}'
                    if mode == "bad_event":
                        stdout = b'{"type":"item.completed","item":[]}'
                    return subprocess.CompletedProcess(command, 1 if mode == "nonzero" else 0, stdout, b"")
                transport = CodexTransport(settings, runner=runner)
                if mode == "good":
                    self.assertEqual(len(parse_records(transport.request(paths, "context", "system", OUTPUT_SCHEMA).content, 2)), 2)
                else:
                    with self.subTest(mode=mode), self.assertRaises((VisionError, subprocess.TimeoutExpired)):
                        parse_records(transport.request(paths, "context", "system", OUTPUT_SCHEMA).content, 2)
            self.assertEqual(len(set(output_paths)), len(output_paths))
            self.assertTrue(all(not p.parent.exists() for p in output_paths))
