from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wechat_diary_core.config import load_config
from wechat_diary_core.preprocessing import run


class FakeOcrEngine:
    def read_text(self, image_path: Path, min_confidence: float) -> list[str]:
        return ["image text"]


class PreprocessingTests(unittest.TestCase):
    def test_clean_filter_ocr_and_compress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "Chat_20260513" / "media" / "images" / "a.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"fake")
            export_path = root / "Chat_20260513" / "Chat_20260513.json"
            export_path.write_text(
                json.dumps(
                    {
                        "weflow": {},
                        "session": {"type": "群聊", "messageCount": 5},
                        "messages": [
                            {"localId": 1, "createTime": 1000, "formattedTime": "2026-05-13 00:00:00", "type": "文本消息", "content": "near", "isSend": 0, "senderUsername": "a"},
                            {"localId": 2, "createTime": 1010, "formattedTime": "2026-05-13 00:00:10", "type": "文本消息", "content": "mine", "isSend": 1, "senderUsername": "me"},
                            {"localId": 3, "createTime": 1020, "formattedTime": "2026-05-13 00:00:20", "type": "文本消息", "content": "again", "isSend": 1, "senderUsername": "me"},
                            {"localId": 4, "createTime": 1030, "formattedTime": "2026-05-13 00:00:30", "type": "图片消息", "content": "media/images/a.jpg", "isSend": 0, "senderUsername": "a"},
                            {"localId": 5, "createTime": 1040, "formattedTime": "2026-05-13 00:00:40", "type": "动画表情", "content": "[表情包]", "isSend": 0, "senderUsername": "a"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            cfg = load_config(root / "missing.toml")
            exports = run(root, config=cfg, ocr_engine=FakeOcrEngine())

        self.assertEqual(len(exports), 1)
        messages = exports[0].data["messages"]
        self.assertEqual(messages[1]["content"], "mine\nagain")
        self.assertIn("[OCR] image text", messages[2]["content"])
        self.assertEqual(messages[3]["content"], "[表情]")


if __name__ == "__main__":
    unittest.main()
