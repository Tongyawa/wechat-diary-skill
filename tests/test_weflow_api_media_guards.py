from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import export_on_demand
from wechat_diary_core.backends.weflow_api import mapper
from wechat_diary_core.backends.weflow_api.backend import WeflowApiBackend
from wechat_diary_core.backends.weflow_api.client import WeflowApiClient
from wechat_diary_core.config import load_config


# Field order/types, sender prefix and XML tag/attribute shapes were observed
# in real API pages. All identities, contents, paths and bytes below are synthetic.
SESSION = {"username": "session_placeholder", "displayName": "Fixture"}
DAY = date(2026, 3, 3)
NEXT_DAY = date(2026, 3, 4)


class PageClient(WeflowApiClient):
    def __init__(self, pages):
        super().__init__("http://example.invalid", "fixture-token")
        self.pages = pages

    def health(self):
        return {"status": "ok"}

    def validate_token(self):
        pass

    def fetch_sessions(self, **kwargs):
        return [SESSION]

    def fetch_contacts(self, **kwargs):
        return [SESSION]

    def get_message_page(self, talker, *, offset=0, media=True, **kwargs):
        def in_range(message):
            day = datetime.fromtimestamp(message['createTime']).date()
            start, end = kwargs.get('start'), kwargs.get('end')
            return (start is None or day >= start) and (end is None or day <= end)
        pages = [[m for m in page if in_range(m)] for page in self.pages]
        cursor = 0
        for index, rows in enumerate(pages):
            if offset == cursor:
                page = copy.deepcopy(rows)
                if not media:
                    for message in page:
                        for key in ("mediaType", "mediaFileName", "mediaUrl", "mediaLocalPath"):
                            message.pop(key, None)
                return {"success": True, "messages": page, "count": len(page),
                        "hasMore": index < len(pages) - 1}
            cursor += len(rows)
        raise AssertionError(f"unexpected offset {offset}")


class MediaGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.serial = 0
        cache = self.root / "cache" / "api-media" / SESSION["username"]
        self.a = cache / "images" / ("a" * 32 + ".png")
        self.b = cache / "images" / ("b" * 32 + ".gif")
        self.emoji = cache / "emojis" / ("c" * 32 + ".gif")
        self.a.parent.mkdir(parents=True)
        self.emoji.parent.mkdir(parents=True)
        self.a.write_bytes(b"image-a")
        self.b.write_bytes(b"image-b-gif")
        self.emoji.write_bytes(b"emoji-c-gif")

    def message(self, base=3, *, path=None, identity=None, local_id=701,
                media_type=None, day=DAY):
        self.serial += 1
        path = self.a if path is None else path
        identity = path.stem if identity is None else identity
        tag = {3: "img", 47: "emoji", 34: "voicemsg", 43: "videomsg"}.get(base)
        raw = f'sender_placeholder:\n<msg><{tag} md5="{identity}" /></msg>' if tag else "plain text"
        media_type = media_type or {3: "image", 47: "emoji", 34: "voice", 43: "video"}.get(base, "image")
        return {
            "localId": local_id, "serverId": f"server-{self.serial}", "localType": base,
            "createTime": int(datetime(day.year, day.month, day.day, 12, 0, self.serial).timestamp()),
            "sortSeq": self.serial, "isSend": 0, "senderUsername": "sender_placeholder",
            "content": "plain text" if base == 1 else "[media]", "rawContent": raw,
            "parsedContent": "[media]", "mediaType": media_type,
            "mediaFileName": path.name, "mediaUrl": "http://example.invalid/media/fixture",
            "mediaLocalPath": str(path),
        }

    def mapped(self, messages, *, transcriber=None):
        stats = mapper.ImageMediaStats()
        data = mapper.map_session_json(SESSION, messages, contacts=[],
                                      media_dir=self.root / "_media", transcriber=transcriber,
                                      image_media_stats=stats)
        return {m["platformMessageId"]: m for m in data["messages"]}, stats

    def sources(self, messages):
        rows, stats = self.mapped(messages)
        return [bool(rows[m["serverId"]]["source"]) for m in messages], stats

    def test_image_emoji_collision_rejects_image_and_preserves_emoji(self):
        messages = [self.message(path=self.emoji, media_type="emoji"),
                    self.message(47, path=self.emoji)]
        rows, stats = self.mapped(messages)
        self.assertEqual(rows[messages[0]["serverId"]]["content"], "[图片]")
        self.assertTrue(rows[messages[1]["serverId"]]["source"].endswith(".gif"))
        self.assertEqual(stats.rejected_media_type_mismatches, 1)
        self.assertEqual(stats.rejected_media_collisions, 0)

    def test_first_image_with_later_text_keeps_good_image_and_text_body(self):
        messages = [self.message(), self.message(1)]
        rows, stats = self.mapped(messages)
        self.assertTrue(rows[messages[0]["serverId"]]["source"])
        self.assertEqual(rows[messages[1]["serverId"]]["source"], "")
        self.assertEqual(rows[messages[1]["serverId"]]["content"], "plain text")
        self.assertEqual(stats.rejected_media_type_mismatches, 1)
        self.assertEqual(stats.rejected_media_collisions, 0)

    def test_same_class_identity_match_one_both_or_neither(self):
        for base in (3, 47):
            for identities, expected in [(("a", "b"), [True, False]),
                                         (("a", "a"), [True, True]),
                                         (("b", "c"), [False, False])]:
                with self.subTest(base=base, identities=identities):
                    messages = [self.message(base, identity=x * 32) for x in identities]
                    sources, stats = self.sources(messages)
                    self.assertEqual(sources, expected)
                    self.assertEqual(stats.rejected_media_collisions, expected.count(False))

    def test_raw_content_identity_not_display_content_and_mutation_changes_result(self):
        first = self.message(identity="b" * 32)
        second = self.message(identity="a" * 32)
        first["content"] = first["parsedContent"] = second["rawContent"]
        self.assertEqual(self.sources([first, second])[0], [False, True])
        first["rawContent"] = second["rawContent"]
        self.assertEqual(self.sources([first, second])[0], [True, True])

    def test_any_missing_or_invalid_identity_rejects_whole_group(self):
        for raw in ("", "broken xml", '<msg><img md5="short" /></msg>',
                    '<msg><img /><other md5="' + "a" * 32 + '" /></msg>'):
            with self.subTest(raw=raw):
                first, second = self.message(), self.message()
                second["rawContent"] = raw
                sources, stats = self.sources([first, second])
                self.assertEqual(sources, [False, False])
                self.assertEqual(stats.rejected_media_collisions, 2)

    def test_fallback_filename_has_no_comparable_identity(self):
        for base, filename in [(3, "image_701.png"), (47, "emoji_701.gif")]:
            path = self.root / filename
            path.write_bytes(b"fallback")
            messages = [self.message(base, path=path, identity="a" * 32) for _ in range(2)]
            self.assertEqual(self.sources(messages)[0], [False, False])

    def test_same_class_voice_and_video_groups_never_use_transcriber(self):
        transcriber = Mock()
        for base, placeholder in [(34, "[语音消息 - 转文字失败: 媒体缺失]"), (43, "[视频]")]:
            messages = [self.message(base) for _ in range(2)]
            rows, stats = self.mapped(messages, transcriber=transcriber)
            self.assertTrue(all(not m["source"] and m["content"] == placeholder for m in rows.values()))
            self.assertEqual(stats.rejected_media_collisions, 2)
        transcriber.transcribe.assert_not_called()

    def test_type_mismatch_matrix_and_missing_type_compatibility(self):
        for base, matching_type in [(3, "image"), (34, "voice"), (43, "video"), (47, "emoji")]:
            for media_type in mapper.MEDIA_TYPE_CLASSES:
                message = self.message(base, media_type=media_type)
                sources, stats = self.sources([message])
                self.assertEqual(sources, [media_type == matching_type])
                self.assertEqual(stats.rejected_media_type_mismatches, int(media_type != matching_type))
            message = self.message(base)
            del message["mediaType"]
            self.assertEqual(self.sources([message])[0], [True])

    def test_mismatched_voice_is_not_transcribed(self):
        transcriber = Mock()
        message = self.message(34, media_type="emoji", path=self.b)
        rows, stats = self.mapped([message], transcriber=transcriber)
        self.assertEqual(rows[message["serverId"]]["content"], "[语音消息 - 转文字失败: 媒体缺失]")
        transcriber.transcribe.assert_not_called()
        self.assertEqual(stats.rejected_media_type_mismatches, 1)

    def test_same_id_different_paths_and_cross_page_are_not_collisions(self):
        self.assertEqual(self.sources([self.message(), self.message(path=self.b)])[0], [True, True])
        messages = [self.message(identity="b" * 32), self.message(identity="c" * 32)]
        client = PageClient([[messages[0]], [messages[1]]])
        checked = client.fetch_messages(SESSION["username"], start=DAY, end=DAY)
        self.assertEqual(self.sources(checked)[0], [True, True])
        # Collapsing the same two responses into one is detectably different.
        self.assertEqual(self.sources(messages)[0], [False, False])

    def test_page_permutations_do_not_change_ownership(self):
        import itertools
        messages = [self.message(identity="b" * 32), self.message(), self.message(1)]
        expected = self.mapped(messages)
        for permutation in itertools.permutations(messages):
            self.assertEqual(self.mapped(permutation), expected)

    def test_same_timestamp_different_server_ids_still_use_identity(self):
        messages = [self.message(), self.message(identity="b" * 32)]
        messages[1]["createTime"] = messages[0]["createTime"]
        checked = PageClient([messages]).fetch_messages(SESSION["username"], start=DAY, end=DAY)
        self.assertEqual(self.sources(checked)[0], [True, False])

    def test_empty_paths_do_not_create_spurious_collision_counts(self):
        messages = [self.message(), self.message()]
        for m in messages:
            m.pop("mediaLocalPath")
            m.pop("mediaType")
        sources, stats = self.sources(messages)
        self.assertEqual(sources, [False, False])
        self.assertEqual(stats.rejected_media_collisions, 0)
        self.assertEqual(stats.missing_image_paths, 2)

    def test_unaffected_gif_and_noncollision_payload_are_byte_identical(self):
        messages = [self.message(path=self.b), self.message(local_id=702)]
        original = copy.deepcopy(messages)
        def run():
            return mapper.map_session_json(SESSION, messages, contacts=[], media_dir=self.root / "_media")
        guarded = json.dumps(run(), ensure_ascii=False).encode()
        before_files = {str(p.relative_to(self.root / '_media')): p.read_bytes()
                        for p in (self.root / '_media').rglob('*') if p.is_file()}
        with patch.object(mapper, "guard_message_page", side_effect=lambda page: list(page)):
            unguarded = json.dumps(run(), ensure_ascii=False).encode()
        self.assertEqual(guarded, unguarded)
        self.assertEqual(messages, original)
        self.assertEqual(before_files, {str(p.relative_to(self.root / '_media')): p.read_bytes()
                                       for p in (self.root / '_media').rglob('*') if p.is_file()})

    def test_checked_messages_keep_wire_fields_without_serialized_metadata(self):
        messages = [self.message(identity="b" * 32), self.message()]
        checked = mapper.guard_message_page(messages)
        self.assertEqual(json.dumps(checked), json.dumps(messages))
        self.assertEqual(self.mapped(copy.deepcopy(checked)), self.mapped(messages))

    def test_unknown_type_does_not_guess_class_and_mixed_legacy_group_is_rejected(self):
        message = self.message()
        message["mediaType"] = "future_kind"
        self.assertEqual(self.sources([message])[0], [True])
        text = self.message(1)
        text.pop("mediaType")
        message.pop("mediaType")
        _, stats = self.mapped([message, text])
        self.assertEqual(stats.rejected_media_collisions, 2)
        self.assertEqual(stats.rejected_media_type_mismatches, 0)

    def test_zero_rejections_are_silent(self):
        backend = WeflowApiBackend(self.config())
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            backend._report_media_rejections(SESSION["username"], "Fixture", mapper.ImageMediaStats())
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(backend.partial_failures, [])

    def config(self):
        path = self.root / "config.toml"
        path.write_text('''[export_backend]
backend = "weflow_api"
[export_backend.weflow_api]
access_token = "fixture-token"
[paths]
raw = "raw"
processed = "processed"
archived = "archived"
insights = "insights"
''', encoding="utf-8")
        return load_config(path)

    def chain_page(self, *, second_day=DAY):
        return [self.message(identity="b" * 32), self.message(1), self.message(day=second_day)]

    def assert_chain_output(self, root, messages, *, split=False):
        rows = {}
        for p in root.glob("*/*.json"):
            rows.update({m["platformMessageId"]: m for m in json.loads(p.read_text(encoding="utf-8"))["messages"]})
        self.assertEqual(len(rows), 3)
        self.assertEqual(bool(rows[messages[0]["serverId"]]["source"]), split)
        self.assertFalse(rows[messages[1]["serverId"]]["source"])
        self.assertTrue(rows[messages[2]["serverId"]]["source"])

    def test_daily_entry_and_console_counts(self):
        cfg = self.config()
        messages = self.chain_page()
        client = PageClient([messages])
        backend = WeflowApiBackend(cfg, client_factory=lambda *a, **kw: client)
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            backend.export_chats(DAY)
        self.assert_chain_output(cfg.paths.raw, messages)
        warnings = [line for line in stderr.getvalue().splitlines() if "已拒绝媒体引用" in line]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].startswith("[WARN]"))
        self.assertLess(len(warnings[0]), 240)
        self.assertIn("归属不符或无法判定 1 条", warnings[0])
        self.assertIn("媒体类别不符 1 条", warnings[0])
        self.assertIn("export_chat_media_rejected:session_placeholder:collision=1:type_mismatch=1:same_day=2:voice_cache=0", backend.partial_failures)

    def test_bulk_entry_keeps_cross_day_page_decisions(self):
        cfg = self.config()
        messages = self.chain_page(second_day=NEXT_DAY)
        client = PageClient([messages])
        backend = WeflowApiBackend(cfg, client_factory=lambda *a, **kw: client)
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            result = backend.export_chats_range(DAY, NEXT_DAY, raw_root=self.root / "bulk",
                                                state_path=self.root / "bulk-state.json")
        self.assertEqual(result.failures, {})
        self.assertEqual(result.messages_written, 3)
        self.assert_chain_output(self.root / "bulk", messages, split=True)
        self.assertEqual(result.media_split_segments, 2)
        self.assertEqual(result.rejected_same_day_collisions, 1)
        self.assertEqual(result.rejected_voice_cache, 0)
        self.assertIn("export_chat_media_rejected:session_placeholder:collision=0:type_mismatch=1:same_day=1:voice_cache=0", backend.partial_failures)

    def test_on_demand_cli_uses_guard_and_reports_pre_backfill_counts(self):
        cfg = self.config()
        messages = self.chain_page(second_day=NEXT_DAY)
        client = PageClient([messages])
        stderr = io.StringIO()
        with patch.object(export_on_demand, "WeflowApiClient", return_value=client), \
                redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            result = export_on_demand.main(["--config", str(cfg.config_path),
                "--session", SESSION["username"], "--start", str(DAY), "--end", str(NEXT_DAY),
                "--out", str(self.root / "on-demand"), "--no-asr", "--no-media-copy"])
        self.assertEqual(result, 0, stderr.getvalue())
        self.assert_chain_output(self.root / "on-demand" / "_raw", messages, split=True)
        self.assertIn("归属不符或无法判定 0 条", stderr.getvalue())
        self.assertIn("媒体类别不符 1 条", stderr.getvalue())

    def test_voice_cache_counts_reach_all_three_entries(self):
        cfg = self.config()
        voice = self.root / 'voice_701.wav'
        voice.write_bytes(b'synthetic-old-voice')
        os.utime(voice, ns=(1_000_000_000, 1_000_000_000))
        messages = [self.message(34, path=voice), self.message(34, path=voice, day=NEXT_DAY)]
        backend = WeflowApiBackend(cfg, client_factory=lambda *a, **kw: PageClient([messages]))
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            backend.export_chats(NEXT_DAY)
        self.assertTrue(any(':voice_cache=1' in m for m in backend.partial_failures))
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            bulk = backend.export_chats_range(DAY, NEXT_DAY, raw_root=self.root / 'voice-bulk',
                                             state_path=self.root / 'voice-bulk-state.json')
        self.assertEqual(bulk.failures, {})
        self.assertEqual(bulk.media_split_segments, 2)
        self.assertEqual(bulk.rejected_voice_cache, 2)
        self.assertEqual(bulk.messages_written, 2)
        stderr = io.StringIO()
        with patch.object(export_on_demand, 'WeflowApiClient', return_value=PageClient([messages])), \
                redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            rc = export_on_demand.main(['--config', str(cfg.config_path), '--session', SESSION['username'],
                '--start', str(NEXT_DAY), '--end', str(NEXT_DAY), '--out', str(self.root / 'voice-on-demand'),
                '--no-asr', '--no-media-copy'])
        self.assertEqual(rc, 0)
        self.assertIn('语音缓存归属无法证明 1 条', stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
