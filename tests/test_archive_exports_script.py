from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from scripts.archive_exports import main as archive_exports_main


def _write_config(root: Path) -> Path:
    config_path = root / "config.toml"
    config_path.write_text(
        f"""
[paths]
raw = "{(root / 'raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
archived = "{(root / 'archived').as_posix()}"

[automation]
weflow_exe = "{(root / 'WeFlow.exe').as_posix()}"
""".strip(),
        encoding="utf-8",
    )
    return config_path


def _chat_payload(*, session_wxid: str = "wxid_a") -> dict:
    return {
        "weflow": {},
        "session": {
            "wxid": session_wxid,
            "nickname": "A",
            "remark": "",
            "displayName": "A",
            "type": "私聊",
        },
        "messages": [
            {
                "localId": 1,
                "createTime": 1778839200,
                "formattedTime": "2026-05-15 10:00:00",
                "type": "文本消息",
                "content": "hello",
                "source": "",
                "isSend": 0,
                "senderUsername": session_wxid,
                "senderDisplayName": "A",
                "platformMessageId": "p1",
            }
        ],
    }


class ArchiveExportsScriptTests(unittest.TestCase):
    def test_ingests_backfill_raw_and_processed_and_removes_emptied_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            raw_backfill = root / "WeFlow-raw-exports-去年"
            (raw_backfill / "私聊_A_20250606-20260606").mkdir(parents=True)
            (raw_backfill / "私聊_A_20250606-20260606" / "私聊_A_20250606-20260606.json").write_text(
                json.dumps(_chat_payload(), ensure_ascii=False), encoding="utf-8"
            )
            processed_backfill = root / "WeFlow-processed-exports-去年"
            (processed_backfill / "私聊_A").mkdir(parents=True)
            (processed_backfill / "私聊_A" / "2025-07-01.md").write_text("hi", encoding="utf-8")

            exit_code = archive_exports_main(
                [
                    "--config",
                    str(config_path),
                    "--raw-root",
                    str(raw_backfill),
                    "--processed-root",
                    str(processed_backfill),
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(
                (root / "archived" / "raw" / "私聊_A" / "私聊_A_20250606-20260606.json").exists()
            )
            self.assertTrue((root / "archived" / "processed" / "私聊_A" / "2025-07-01.md").exists())
            self.assertFalse(raw_backfill.exists())
            self.assertFalse(processed_backfill.exists())

    def test_raw_schema_failure_skips_bad_json_and_keeps_source_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            raw_backfill = root / "snapshot-raw"
            good = raw_backfill / "私聊_A_20260611" / "私聊_A_20260611.json"
            bad = raw_backfill / "私聊_B_20260611" / "私聊_B_20260611.json"
            good.parent.mkdir(parents=True)
            bad.parent.mkdir(parents=True)
            good.write_text(json.dumps(_chat_payload(session_wxid="wxid_a"), ensure_ascii=False), encoding="utf-8")
            invalid = _chat_payload(session_wxid="wxid_b")
            invalid["session"].pop("wxid")
            bad.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = archive_exports_main(["--config", str(config_path), "--raw-root", str(raw_backfill)])

            self.assertEqual(exit_code, 1)
            self.assertTrue((root / "archived" / "raw" / "私聊_A" / "私聊_A_20260611.json").exists())
            self.assertFalse((root / "archived" / "raw" / "私聊_B" / "私聊_B_20260611.json").exists())
            self.assertTrue(raw_backfill.exists())
            self.assertIn("session.wxid", stderr.getvalue())

    def test_keep_source_leaves_the_backfill_tree_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            processed_backfill = root / "snapshot-processed"
            (processed_backfill / "私聊_A").mkdir(parents=True)
            src = processed_backfill / "私聊_A" / "2025-07-01.md"
            src.write_text("hi", encoding="utf-8")

            exit_code = archive_exports_main(
                ["--config", str(config_path), "--processed-root", str(processed_backfill), "--keep-source"]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(src.exists())
            self.assertTrue((root / "archived" / "processed" / "私聊_A" / "2025-07-01.md").exists())


if __name__ == "__main__":
    unittest.main()
