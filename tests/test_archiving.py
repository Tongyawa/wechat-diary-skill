from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wechat_diary_core.archiving import archive, strip_date_suffix
from wechat_diary_core.preprocessing.cleaner import ProcessedChatExport


class ArchivingTests(unittest.TestCase):
    def test_strip_date_suffix(self) -> None:
        self.assertEqual(strip_date_suffix("Chat_20260513"), "Chat")
        self.assertEqual(strip_date_suffix("Chat_20260512-20260514"), "Chat")

    def test_archive_groups_by_message_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_root = Path(tmp) / "processed"
            export = ProcessedChatExport(
                source_path=Path(tmp) / "Chat_20260513" / "Chat_20260513.json",
                source_folder="Chat_20260513",
                data={
                    "weflow": {},
                    "session": {"type": "私聊", "messageCount": 2},
                    "messages": [
                        {"formattedTime": "2026-05-13 23:59:00", "content": "a"},
                        {"formattedTime": "2026-05-14 00:01:00", "content": "b"},
                    ],
                },
            )

            paths = archive([export], output_root=out_root)

            first = json.loads((out_root / "Chat" / "2026-05-13.json").read_text(encoding="utf-8"))
            second = json.loads((out_root / "Chat" / "2026-05-14.json").read_text(encoding="utf-8"))

        self.assertEqual(len(paths), 2)
        self.assertEqual(first["messages"][0]["content"], "a")
        self.assertEqual(second["messages"][0]["content"], "b")


if __name__ == "__main__":
    unittest.main()
