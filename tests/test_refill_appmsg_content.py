from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import refill_appmsg_content as tool
from wechat_diary_core.backends.weflow_api.mapper import map_session_json


FIXTURE = Path(__file__).parent / "fixtures" / "weflow_media_appmsg.json"
TALKER = "wxid_contact_placeholder"
STAMP = int(datetime(2026, 5, 12, 10).timestamp())


def api_message(local=1, *, app=19, stamp=STAMP, title="Forward title"):
    return {"localId": local, "serverId": f"server-{local}", "createTime": stamp,
            "localType": (app << 32) | 49, "senderUsername": TALKER, "isSend": 0,
            "content": "[聊天记录]", "rawContent": f"<msg><appmsg><title>{title}</title>"
            f"<des>Summary</des><type>{app}</type></appmsg></msg>"}


def canonical(messages):
    return map_session_json({"username": TALKER}, messages, contacts=[], require_media=False)


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def fetch_messages(self, talker, *, start, end, media):
        self.calls.append((talker, start, end, media))
        return [deepcopy(m) for m in self.messages if datetime.fromtimestamp(m["createTime"]).date() == start]


class RefillAppmsgTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "raw"
        self.path = self.root / "Private Placeholder" / "range.json"
        self.path.parent.mkdir(parents=True)
        self.backup = self.base / "backup"
        self.api = [api_message()]
        self.payload = canonical(self.api)
        self.payload["messages"][0]["content"] = "[合并转发]"

    def write(self, payload=None, *, compact=False, bom=False):
        raw = json.dumps(payload or self.payload, ensure_ascii=compact,
                         indent=None if compact else 3).replace("\n", "\r\n").encode("utf-8") + b"\r\n\t"
        if bom:
            raw = b"\xef\xbb\xbf" + raw
        self.path.write_bytes(raw)
        return raw

    def run_tool(self, api=None, **kw):
        client = FakeClient(self.api if api is None else api)
        report = tool.refill(self.root, client=client, **kw)
        return report, client

    def stats(self, report):
        return report["by_type"]["[合并转发]"]

    def test_apply_only_content_bytes_and_restore_original(self):
        self.payload["messages"][0]["source"] = "../_media/files/aa/file.txt"
        self.payload["messages"][0]["extra"] = {"content": "[合并转发]", "ordered": [3, 1]}
        self.payload["messages"] += [
            dict(self.payload["messages"][0], localId=2, content="[语音转文字] Preserved speech", type="语音消息", source="media/voice.wav"),
            dict(self.payload["messages"][0], localId=3, content="../_media/images/aa/patched.jpg", type="图片消息", source="../_media/images/aa/patched.jpg"),
            dict(self.payload["messages"][0], localId=4, content="Preserved text", type="文本消息"),
        ]
        self.payload["avatars"] = {"content": "[合并转发]"}
        before = self.write()
        report, client = self.run_tool(apply=True, backup_dir=self.backup)
        expected = before.replace('"content": "[合并转发]"'.encode(),
                                  '"content": "[合并转发] Forward title：Summary"'.encode(), 1)
        self.assertEqual(expected, self.path.read_bytes())
        self.assertEqual(1, report["written_files"])
        self.assertEqual(1, self.stats(report)["refilled"])
        self.assertEqual([(TALKER, datetime.fromtimestamp(STAMP).date(), datetime.fromtimestamp(STAMP).date(), False)], client.calls)
        saved = self.backup / self.path.relative_to(self.root)
        self.assertEqual(before, saved.read_bytes())
        tool.atomic_replace_bytes(self.path, saved.read_bytes())
        self.assertEqual(before, self.path.read_bytes())

    def test_compact_escaped_bom_and_key_order_preserved(self):
        before = self.write(compact=True, bom=True)
        report, _ = self.run_tool(apply=True, backup_dir=self.backup)
        after = self.path.read_bytes()
        self.assertTrue(after.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(1, report["written_files"])
        tool.verify_patch(before, after, {0: "[合并转发] Forward title：Summary"})

    def test_dry_run_no_writes_or_backup_but_positive_plan(self):
        before = self.write()
        report, _ = self.run_tool(backup_dir=self.backup)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(self.backup.exists())
        self.assertEqual(0, report["written_files"])
        self.assertEqual(1, self.stats(report)["refillable"])

    def test_idempotent_no_requests_second_time(self):
        self.write()
        self.run_tool(apply=True, backup_dir=self.backup)
        before = self.path.read_bytes()
        report, client = self.run_tool(apply=True, backup_dir=self.backup)
        self.assertEqual({}, report["by_type"])
        self.assertEqual([], client.calls)
        self.assertEqual(0, report["written_files"])
        self.assertEqual(before, self.path.read_bytes())

    def test_wrong_identity_refuses_whole_file_with_positive_control(self):
        for field, wrong in [("serverId", "different"), ("senderUsername", "wxid_wrong_placeholder"),
                             ("isSend", 1), ("localType", (33 << 32) | 49), ("talker", "wrong_talker")]:
            with self.subTest(field=field):
                second = api_message(2)
                self.payload = canonical([self.api[0], second])
                for m in self.payload["messages"]:
                    m["content"] = "[合并转发]"
                before = self.write()
                bad = dict(self.api[0], **{field: wrong})
                report, _ = self.run_tool([bad, second], apply=True, backup_dir=self.backup)
                self.assertEqual(before, self.path.read_bytes())
                self.assertEqual(2, self.stats(report)["rejected"])
                self.assertFalse(self.backup.exists())
                good, _ = self.run_tool([self.api[0], second])
                self.assertEqual(2, self.stats(good)["refillable"])

    def test_out_of_scope_base_does_not_block_same_file_appmsg(self):
        for base in (66, 11000):
            for apply in (False, True):
                with self.subTest(base=base, apply=apply):
                    outside = dict(api_message(), localType=base, content="Opaque payload")
                    good = api_message(2)
                    self.payload = canonical([outside, good])
                    self.payload["messages"][0]["source"] = "media/keep.bin"
                    self.payload["messages"][0]["extra"] = {"content": "[其他消息]", "keep": [7, 3]}
                    self.payload["messages"][1]["content"] = "[合并转发]"
                    before = self.write()
                    backup = self.base / f"backup-{base}-{apply}"
                    with patch.object(tool, "_message_content", wraps=tool._message_content) as render:
                        report, client = self.run_tool([outside, good], apply=apply, backup_dir=backup)
                    self.assertEqual(int(apply), report["written_files"])
                    self.assertEqual(1, report["by_type"]["[其他消息]"]["still_bare"])
                    self.assertEqual(0, report["by_type"]["[其他消息]"]["rejected"])
                    self.assertEqual(1, self.stats(report)["refilled" if apply else "refillable"])
                    self.assertEqual(0, self.stats(report)["rejected"])
                    self.assertEqual(3, tool.exit_code(report))
                    self.assertEqual(1, len(client.calls))
                    self.assertEqual(1, render.call_count)
                    expected = before.replace('"content": "[合并转发]"'.encode(),
                                              '"content": "[合并转发] Forward title：Summary"'.encode(), 1) if apply else before
                    self.assertEqual(expected, self.path.read_bytes())
                    self.assertEqual(json.loads(before)["messages"][0], json.loads(self.path.read_bytes())["messages"][0])
                    if apply:
                        self.assertEqual(before, (backup / self.path.relative_to(self.root)).read_bytes())
                        again, _ = self.run_tool([outside, good], apply=True, backup_dir=backup)
                        self.assertEqual(0, again["written_files"])
                        self.assertEqual(1, again["by_type"]["[其他消息]"]["still_bare"])
                    else:
                        self.assertFalse(backup.exists())

    def test_out_of_scope_message_still_requires_matching_identity(self):
        for base in (66, 11000):
            for field, wrong in (("serverId", "wrong-server"), ("senderUsername", "wxid_wrong_placeholder"),
                                 ("isSend", 1), ("localType", 1), ("localType", 42)):
                with self.subTest(base=base, field=field, wrong=wrong):
                    outside = dict(api_message(), localType=base, content="Opaque payload")
                    good = api_message(2)
                    self.payload = canonical([outside, good])
                    self.payload["messages"][1]["content"] = "[合并转发]"
                    before = self.write()
                    bad = dict(outside, **{field: wrong})
                    report, _ = self.run_tool([bad, good], apply=True, backup_dir=self.backup)
                    self.assertEqual(0, report["written_files"])
                    self.assertEqual(before, self.path.read_bytes())
                    self.assertFalse(self.backup.exists())
                    self.assertEqual(1, report["by_type"]["[其他消息]"]["rejected"])
                    self.assertEqual(1, self.stats(report)["rejected"])
                    self.assertEqual(1, tool.exit_code(report))

    def test_only_out_of_scope_messages_are_still_bare_without_writing(self):
        for base in (66, 11000):
            with self.subTest(base=base):
                outside = dict(api_message(), localType=base, content="Opaque payload")
                self.payload = canonical([outside])
                before = self.write()
                with patch.object(tool, "_message_content", side_effect=AssertionError("must not render unsupported base")):
                    report, _ = self.run_tool([outside], apply=True, backup_dir=self.backup)
                self.assertEqual(1, report["by_type"]["[其他消息]"]["still_bare"])
                self.assertEqual(0, report["by_type"]["[其他消息]"]["rejected"])
                self.assertEqual(3, tool.exit_code(report))
                self.assertEqual(0, report["written_files"])
                self.assertEqual(before, self.path.read_bytes())
                self.assertFalse(self.backup.exists())

    def test_missing_message_does_not_abort_other_files(self):
        before = self.write()
        other = self.root / "Other" / "other.json"
        other.parent.mkdir()
        other_payload = canonical([api_message(2)])
        other_payload["messages"][0]["content"] = "[合并转发]"
        other.write_text(json.dumps(other_payload), encoding="utf-8")
        report, _ = self.run_tool([api_message(2)], apply=True, backup_dir=self.backup)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(1, self.stats(report)["missing"])
        self.assertEqual(1, self.stats(report)["refilled"])
        self.assertEqual(3, tool.exit_code(report))

    def test_still_bare_no_write_and_positive_control(self):
        before = self.write()
        empty = dict(self.api[0], rawContent="<msg><appmsg><title/><type>19</type></appmsg></msg>")
        report, _ = self.run_tool([empty], apply=True, backup_dir=self.backup)
        self.assertEqual(1, self.stats(report)["still_bare"])
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(self.backup.exists())
        good, _ = self.run_tool()
        self.assertEqual(1, self.stats(good)["refillable"])

    def test_filtered_video_title_is_not_success(self):
        fresh = api_message(app=63, title="当前微信版本不支持展示该内容，请升级至最新版本")
        self.payload = canonical([fresh])
        self.payload["messages"][0]["content"] = "[视频号]"
        before = self.write()
        report, _ = self.run_tool([fresh], apply=True, backup_dir=self.backup)
        self.assertEqual(1, report["by_type"]["[视频号]"]["still_bare"])
        self.assertEqual(before, self.path.read_bytes())

    def test_duplicate_identity_rejected_not_collapsed(self):
        self.write()
        report, _ = self.run_tool(self.api * 2, apply=True, backup_dir=self.backup)
        self.assertEqual(1, self.stats(report)["rejected"])
        good, _ = self.run_tool()
        self.assertEqual(1, self.stats(good)["refillable"])
        self.payload["messages"] *= 2
        before = self.write()
        report, _ = self.run_tool(apply=True, backup_dir=self.backup)
        self.assertEqual(2, self.stats(report)["rejected"])
        self.assertEqual(before, self.path.read_bytes())

    def test_without_platform_id_requires_unique_local_time_and_sender(self):
        self.payload["messages"][0].pop("platformMessageId")
        self.write()
        report, _ = self.run_tool()
        self.assertEqual(1, self.stats(report)["refillable"])
        report, _ = self.run_tool([dict(self.api[0], senderUsername="wrong_sender")])
        self.assertEqual(1, self.stats(report)["rejected"])

    def test_changed_timestamp_or_local_id_is_missing_not_misassigned(self):
        before = self.write()
        for bad in [dict(self.api[0], localId=2), dict(self.api[0], createTime=STAMP+1)]:
            report, _ = self.run_tool([bad], apply=True, backup_dir=self.backup)
            self.assertEqual(1, self.stats(report)["missing"])
            self.assertEqual(before, self.path.read_bytes())

    def test_range_requests_only_candidate_days_not_per_message(self):
        second_day = int((datetime.fromtimestamp(STAMP) + timedelta(days=4)).timestamp())
        third_day = int((datetime.fromtimestamp(STAMP) + timedelta(days=9)).timestamp())
        api = [api_message(), api_message(2), api_message(3, stamp=second_day), api_message(4, stamp=third_day)]
        self.payload = canonical(api)
        for m in self.payload["messages"][:3]:
            m["content"] = "[合并转发]"
        self.write()
        report, client = self.run_tool(api)
        self.assertEqual(2, len(client.calls))
        self.assertTrue(all(not call[3] and call[1] == call[2] for call in client.calls))
        self.assertEqual(3, self.stats(report)["refillable"])
        self.assertEqual(2, report["session_days"])

    def test_scan_only_never_uses_client_config_or_writes(self):
        before = self.write()
        with patch.object(tool, "WeflowApiClient", side_effect=AssertionError), patch.object(tool, "load_config", side_effect=AssertionError):
            with patch("sys.stdout", new=io.StringIO()) as stdout:
                self.assertEqual(0, tool.main(["--raw-root", str(self.root), "--scan-only"]))
            report = json.loads(stdout.getvalue().splitlines()[-1])
        self.assertEqual(1, self.stats(report)["candidate"])
        self.assertEqual(0, report["api_requests"])
        self.assertEqual(before, self.path.read_bytes())

    def test_api_failure_isolated_and_no_exception_body_in_report(self):
        before = self.write()
        client = FakeClient([])
        with patch.object(client, "fetch_messages", side_effect=RuntimeError("SENSITIVE_BODY")):
            report = tool.refill(self.root, client=client, apply=True, backup_dir=self.backup)
        self.assertEqual(1, self.stats(report)["api_failed"])
        self.assertEqual(1, tool.exit_code(report))
        self.assertNotIn("SENSITIVE_BODY", json.dumps(report))
        self.assertEqual(before, self.path.read_bytes())
        good, _ = self.run_tool()
        self.assertEqual(1, self.stats(good)["refillable"])

    def test_byte_guard_rejects_unrelated_mutations_with_two_shapes(self):
        before = self.write()
        replacements = {0: "[合并转发] Recovered"}
        good = tool.patch_content(before, replacements)
        tool.verify_patch(before, good, replacements)
        for bad in [good.replace(b'"source": ""', b'"source": "lost"'),
                    good.replace(b'   "session"', b'  "session"'),
                    good + b"\n"]:
            with self.assertRaises(tool.RefillError):
                tool.verify_patch(before, bad, replacements)
        # A change to another message's content must also fail, although content
        # is the allowed field in the repaired message.
        payload = deepcopy(self.payload)
        payload["messages"].append(dict(payload["messages"][0], localId=3, content="keep", type="文本消息"))
        before = self.write(payload)
        good = tool.patch_content(before, replacements)
        with self.assertRaises(tool.RefillError):
            tool.verify_patch(before, good.replace(b'"keep"', b'"different"'), replacements)

    def test_runtime_gate_refuses_corrupt_patch_and_positive_control(self):
        before = self.write()
        original_patch = tool.patch_content
        def bad_patch(raw, changes):
            return original_patch(raw, changes).replace(b'"source": ""', b'"source": "bad"')
        with patch.object(tool, "patch_content", side_effect=bad_patch):
            report, _ = self.run_tool(apply=True, backup_dir=self.backup)
        self.assertEqual(1, self.stats(report)["rejected"])
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(self.backup.exists())
        good, _ = self.run_tool()
        self.assertEqual(1, self.stats(good)["refillable"])

    def test_concurrent_raw_change_is_not_overwritten(self):
        before = self.write()
        client = FakeClient(self.api)
        fetch = client.fetch_messages
        def mutate(*args, **kwargs):
            self.path.write_bytes(before + b" ")
            return fetch(*args, **kwargs)
        client.fetch_messages = mutate
        report = tool.refill(self.root, client=client, apply=True, backup_dir=self.backup)
        self.assertEqual(before + b" ", self.path.read_bytes())
        self.assertEqual(1, self.stats(report)["rejected"])
        self.assertFalse(self.backup.exists())

    def test_backup_conflict_or_failure_prevents_write(self):
        before = self.write()
        saved = self.backup / self.path.relative_to(self.root)
        saved.parent.mkdir(parents=True)
        saved.write_bytes(b"previous original")
        report, _ = self.run_tool(apply=True, backup_dir=self.backup)
        self.assertEqual(1, self.stats(report)["rejected"])
        self.assertEqual(b"previous original", saved.read_bytes())
        self.assertEqual(before, self.path.read_bytes())
        saved.write_bytes(before)
        report, _ = self.run_tool(apply=True, backup_dir=self.backup)
        self.assertEqual(1, self.stats(report)["refilled"])

    def test_backup_required_and_must_be_separate(self):
        self.write()
        for backup in [None, self.root / "backup", self.base, tool.ROOT / "backup"]:
            with self.assertRaises(tool.RefillError):
                self.run_tool(apply=True, backup_dir=backup)
        with self.assertRaises(tool.RefillError):
            self.run_tool(apply=True, scan_only=True, backup_dir=self.backup)
        self.assertEqual(1, self.stats(self.run_tool()[0])["refillable"])

    def test_all_paired_fixtures_and_location_use_current_mapper(self):
        api = []
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        for i, case in enumerate(cases, 1):
            fresh = api_message(i, app=case["app"])
            fresh.update(content=case["content"], rawContent=case["rawContent"])
            api.append(fresh)
        api.append(dict(api_message(99), localType=48,
                        content="<msg><location label='Placeholder road'/></msg>", rawContent=""))
        self.payload = canonical(api)
        for old, fresh in zip(self.payload["messages"], api):
            old["content"] = tool.resolve_message_type(fresh["localType"]).placeholder
        self.write()
        report, _ = self.run_tool(api, apply=True, backup_dir=self.backup)
        after = json.loads(self.path.read_bytes())
        self.assertEqual([c["expected"] for c in cases] + ["[位置] Placeholder road"],
                         [m["content"] for m in after["messages"]])
        self.assertEqual(1, report["written_files"])

    def test_legacy_nonbare_non_appmsg_and_processed_are_untouched(self):
        self.payload["weflow"]["source"] = "legacy"
        before = self.write()
        report, client = self.run_tool(apply=True, backup_dir=self.backup)
        self.assertEqual([], client.calls)
        self.assertEqual(before, self.path.read_bytes())
        self.payload["weflow"]["source"] = "http_api"
        self.payload["messages"][0]["type"] = "文本消息"
        before = self.write()
        processed = self.base / "processed" / "day.md"
        processed.parent.mkdir()
        processed.write_bytes(b"Preserved OCR and old rendered text")
        report, client = self.run_tool(apply=True, backup_dir=self.backup)
        self.assertEqual([], client.calls)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(b"Preserved OCR and old rendered text", processed.read_bytes())

    def test_invalid_json_is_reported_without_body_and_continues(self):
        self.write()
        (self.root / "invalid.json").write_text('{"content":"SENSITIVE_BODY",', encoding="utf-8")
        report, _ = self.run_tool()
        self.assertEqual(1, self.stats(report)["refillable"])
        self.assertEqual(1, len(report["errors"]))
        self.assertNotIn("SENSITIVE_BODY", json.dumps(report))

    def test_duplicate_json_keys_rejected_and_never_reserialized(self):
        before = self.write().replace(b'"content": "[', b'"content": "trap", "content": "[', 1)
        self.path.write_bytes(before)
        report, client = self.run_tool(apply=True, backup_dir=self.backup)
        self.assertEqual(1, len(report["errors"]))
        self.assertEqual([], client.calls)
        self.assertEqual(before, self.path.read_bytes())

    def test_atomic_publish_failure_keeps_original_and_backup(self):
        before = self.write()
        with patch.object(tool, "atomic_replace_bytes", side_effect=OSError("publish failed")):
            report, _ = self.run_tool(apply=True, backup_dir=self.backup)
        self.assertEqual(1, self.stats(report)["rejected"])
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(before, (self.backup / self.path.relative_to(self.root)).read_bytes())
        good, _ = self.run_tool(apply=True, backup_dir=self.backup)
        self.assertEqual(1, self.stats(good)["refilled"])

    def test_configured_title_limit_is_used_and_cli_guard_is_actionable(self):
        self.write()
        report, _ = self.run_tool(appmsg_text_max_chars=4, apply=True, backup_dir=self.backup)
        self.assertEqual("[合并转发] Forw…：Summ…", json.loads(self.path.read_bytes())["messages"][0]["content"])
        self.assertEqual(1, self.stats(report)["refilled"])
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            self.assertEqual(2, tool.main(["--raw-root", str(self.root), "--apply"]))
        self.assertIn("--backup-dir", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
