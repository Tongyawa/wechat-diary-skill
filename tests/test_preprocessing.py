from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_diary_core.config import load_config
from wechat_diary_core.preprocessing import run
from wechat_diary_core.preprocessing import image_ocr
from wechat_diary_core.preprocessing.image_ocr import _parse_paddle_result


class FakeOcrEngine:
    def read_text(self, image_path: Path, min_confidence: float) -> list[str]:
        return ["image text"]


class PreprocessingTests(unittest.TestCase):
    def test_parse_paddle_result_dict_shape(self) -> None:
        result = [{"rec_texts": ["hello", "low"], "rec_scores": [0.9, 0.1]}]

        self.assertEqual(_parse_paddle_result(result, 0.5), ["hello"])

    def test_image_ocr_skips_cleanly_when_local_engines_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "media" / "images" / "a.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"fake")
            messages = [{"type": "图片消息", "content": "media/images/a.jpg"}]
            cfg = load_config(root / "missing.toml").preprocessing

            with patch.object(image_ocr, "RapidOcrEngine", side_effect=RuntimeError("missing")), patch.object(
                image_ocr, "PaddleOcrEngine", side_effect=RuntimeError("broken")
            ):
                result = image_ocr.annotate_image_messages(messages, root, cfg)

        self.assertEqual(result, messages)

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
                            {"localId": 2, "createTime": 1010, "formattedTime": "2026-05-13 00:00:10", "type": "文本消息", "content": "mine", "isSend": 1, "senderUsername": "me", "senderDisplayName": "Self", "platformMessageId": "p2"},
                            {"localId": 3, "createTime": 1020, "formattedTime": "2026-05-13 00:00:20", "type": "文本消息", "content": "again", "isSend": 1, "senderUsername": "me"},
                            {"localId": 4, "createTime": 1030, "formattedTime": "2026-05-13 00:00:30", "type": "图片消息", "content": "media/images/a.jpg", "isSend": 0, "senderUsername": "a"},
                            {"localId": 5, "createTime": 1040, "formattedTime": "2026-05-13 00:00:40", "type": "动画表情", "content": "[表情包]", "isSend": 0, "senderUsername": "a", "replyToMessageId": "p2"},
                            {"localId": 6, "createTime": 1050, "formattedTime": "2026-05-13 00:00:50", "type": "文本消息", "content": "<!-- ChatRoomTopMsgRequest --> 1@chatroom 2 p 1 u <!-- ChatRoomTopMsgResponse --> b", "isSend": 0, "senderUsername": "room"},
                            {"localId": 7, "createTime": 1060, "formattedTime": "2026-05-13 00:01:00", "type": "文本消息", "content": "pinned", "isSend": 0, "senderUsername": "a"},
                            {"localId": 8, "createTime": 1070, "formattedTime": "2026-05-13 00:01:10", "type": "系统消息", "content": "<!-- ChatRoomTopMsgRequest --> 1@chatroom 1 p 1 u <!-- ChatRoomTopMsgResponse --> d", "isSend": 0, "senderUsername": "room"},
                            {"localId": 9, "createTime": 1080, "formattedTime": "2026-05-13 00:01:20", "type": "其他消息", "content": '"A" 拍了拍 "B"', "isSend": 0, "senderUsername": "room"},
                            {"localId": 10, "createTime": 1090, "formattedTime": "2026-05-13 00:01:30", "type": "其他消息", "content": '"A" 拍了拍 "Self"', "isSend": 0, "senderUsername": "room"},
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
        self.assertEqual(messages[3]["replyContext"]["senderDisplayName"], "Self")
        self.assertEqual(messages[3]["replyContext"]["content"], "mine")
        self.assertTrue(messages[4]["is_chatroom_top_message"])
        self.assertEqual(messages[4]["content"], "pinned")
        self.assertFalse(messages[5].get("is_chatroom_top_message", False))
        self.assertTrue(messages[5]["is_self_related_pat"])
        self.assertNotIn("ChatRoomTopMsgRequest", json.dumps(messages, ensure_ascii=False))
        self.assertNotIn('"A" 拍了拍 "B"', json.dumps(messages, ensure_ascii=False))
        self.assertEqual(messages[5]["content"], '"A" 拍了拍 "Self"')


if __name__ == "__main__":
    unittest.main()
