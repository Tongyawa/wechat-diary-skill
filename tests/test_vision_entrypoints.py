from __future__ import annotations
from copy import deepcopy
from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from scripts.export_on_demand import export_on_demand
from scripts.process_existing_raw import ProcessExistingRawDeps, process_existing_raw
from scripts.run_daily_export import DailyExportDeps, run_daily_export
from wechat_diary_core.archiving import archive
from wechat_diary_core.config import load_config
from wechat_diary_core.preprocessing.cleaner import prepare_chat_messages
from test_image_vision import FakeTransport


class EntrypointTests(unittest.TestCase):
    def test_three_real_entrypoints_share_cached_description_context_and_preserve_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            session_dir = raw / "私聊_SessionAlpha_20260813"
            image = session_dir / "media/images/a.jpg"
            image.parent.mkdir(parents=True)
            Image.new("RGB", (600, 900), (10, 30, 90)).save(image)
            source = session_dir / "session.json"
            source.write_text(json.dumps(dict(weflow={}, session=dict(wxid="wxid_entry_contract", nickname="SessionAlpha",
                remark="", displayName="SessionAlpha", type="私聊", messageCount=1), messages=[dict(localId=1,
                createTime=1786579200, formattedTime="2026-08-13 08:00:00", type="图片消息", content="media/images/a.jpg",
                source="", isSend=0, senderUsername="wxid_entry_contract", senderDisplayName="SessionAlpha", platformMessageId="image-1")]), ensure_ascii=False), encoding="utf-8")
            before = source.read_bytes()
            config_path = root / "config.toml"
            config_path.write_text(f'''[paths]
raw="{raw.as_posix()}"
processed="{(root / 'processed').as_posix()}"
archived="{(root / 'archived').as_posix()}"
insights="{(root / 'insights').as_posix()}"
[export_backend]
backend="manual"
[preprocessing]
image_ocr_enabled=false
[preprocessing.image_vision]
enabled=true
[preprocessing.image_label]
enabled=false
[daily_export]
target_usernames=[]
self_moments_usernames=[]
''', encoding="utf-8")
            cfg = load_config(config_path)
            fake = FakeTransport()
            class ManualBackend:
                name = "manual"
                partial_failures = []
            class EmptyClient:
                def fetch_contacts(self, **kwargs): return []
                def fetch_group_members(self, *args): return []
                def fetch_messages(self, *args, **kwargs): return []
            with patch("wechat_diary_core.preprocessing.image_vision.CheapApiVisionDescriber", return_value=fake):
                daily = run_daily_export(cfg, day=date(2026, 8, 13), deps=DailyExportDeps(
                    backend=ManualBackend(), archive_existing_processed=lambda *args, **kwargs: None))
                daily_text = daily.diary_files[0].read_text(encoding="utf-8")
                rerun = process_existing_raw(cfg, raw_root=raw, day="2026-08-13", deps=ProcessExistingRawDeps())
                rerun_text = rerun.diary_files[0].read_text(encoding="utf-8")
                def same_raw(_root, **kwargs): return archive(raw, **kwargs)
                demand = export_on_demand(cfg, sessions=[dict(username="wxid_entry_contract", displayName="SessionAlpha")],
                    client=EmptyClient(), session_query="wxid_entry_contract", start=date(2026, 8, 13), end=date(2026, 8, 13),
                    out_root=root / "on-demand", copy_media=False, enable_asr=False, archive_fn=same_raw)
                demand_text = demand.diary_files[0].read_text(encoding="utf-8")
            def description(body):
                value = body.split("[图片：", 1)[1].split("]", 1)[0]
                return value.split("｜", 1)[1] if value.startswith("media/") else value
            self.assertEqual([description(x) for x in (daily_text, rerun_text, demand_text)], ["照片｜一张合影"] * 3)
            self.assertIn("[图片：media/images/a.jpg｜", demand_text)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(len({c["context"] for c in fake.calls}), 1)
            # Per-entrypoint output isolation may use a separate cache; either
            # way each emitted context must be byte-identical.
            entries = [json.loads(p.read_text(encoding="utf-8")) for p in root.rglob("*.json") if p != source and "_image-descriptions" in p.parts and "_signals" not in p.parts]
            self.assertTrue(entries)
            self.assertEqual(len({e["context_sha256"] for e in entries}), 1)

    def test_disabled_switch_byte_snapshot_and_no_label_image_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw/私聊_SessionAlpha"
            raw.mkdir(parents=True)
            (raw / "data.json").write_text(json.dumps(dict(weflow={}, session=dict(wxid="wxid_alpha", type="私聊", displayName="Alpha", nickname="Alpha", remark=""),
                messages=[dict(localId=1, createTime=1786579200, formattedTime="2026-08-13 08:00:00", type="图片消息",
                content="media/images/missing.png", source="", isSend=0, senderUsername="wxid_alpha", senderDisplayName="Alpha")])), encoding="utf-8")
            cfg = load_config(root / "missing.toml")
            cfg = replace(cfg, paths=replace(cfg.paths, raw=root / "raw", processed=root / "processed", archived=root / "archive"),
                          preprocessing=replace(cfg.preprocessing, image_ocr_enabled=False))
            with patch("wechat_diary_core.preprocessing.image_vision._collect", side_effect=AssertionError("vision disabled")), \
                 patch("wechat_diary_core.preprocessing.vision_service.CodexTransport.request", side_effect=AssertionError("no call")):
                path = archive(root / "raw", config=cfg)[0]
                first = path.read_bytes()
                path = archive(root / "raw", config=cfg)[0]
                self.assertEqual(path.read_bytes(), first)
                self.assertIn("[图片]".encode(), first)
                self.assertNotIn("｜".encode(), first)

    def test_shared_group_window_filters_before_grouping_and_never_changes_raw(self):
        cfg = load_config(Path("missing.toml"))
        cfg = replace(cfg, user=replace(cfg.user, self_wxids=["self"]), preprocessing=replace(cfg.preprocessing,
            group_context_window=replace(cfg.preprocessing.group_context_window, messages_before=0, messages_after=0, time_window_minutes=1)))
        data = dict(session=dict(type="群聊"), messages=[
            dict(type="文本消息", content="window outside", senderUsername="peer", createTime=1000, formattedTime="2026-10-18 10:00:00"),
            dict(type="文本消息", content="anchor", senderUsername="self", isSend=1, createTime=2000, formattedTime="2026-10-18 10:16:40"),
            dict(type="图片消息", content="media/images/a.png", senderUsername="peer", createTime=2005, formattedTime="2026-10-18 10:16:45")])
        before = deepcopy(data)
        output = prepare_chat_messages(data, cfg)
        self.assertEqual([m["content"] for m in output], ["anchor", "media/images/a.png"])
        self.assertEqual(data, before)
        cfg = replace(cfg, preprocessing=replace(cfg.preprocessing,
            group_context_window=replace(cfg.preprocessing.group_context_window, enabled=False)))
        self.assertEqual(len(prepare_chat_messages(data, cfg)), 3)
