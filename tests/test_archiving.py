from __future__ import annotations

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

            first = (out_root / "Chat" / "2026-05-13.md").read_text(encoding="utf-8")
            second = (out_root / "Chat" / "2026-05-14.md").read_text(encoding="utf-8")

        self.assertEqual(len(paths), 2)
        self.assertIn("2026-05-13 23:59:00\n未知：a\n", first)
        self.assertIn("2026-05-14 00:01:00\n未知：b\n", second)

    def test_archive_renders_compact_chat_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_root = Path(tmp) / "processed"
            export = ProcessedChatExport(
                source_path=Path(tmp) / "Chat_20260515" / "Chat_20260515.json",
                source_folder="Chat_20260515",
                data={
                    "session": {"type": "群聊", "messageCount": 6},
                    "messages": [
                        {
                            "localId": 137,
                            "createTime": 1778840003,
                            "formattedTime": "2026-05-15 18:13:23",
                            "type": "文本消息",
                            "content": "公主教就是工学部教1吗？",
                            "isSend": 1,
                            "senderDisplayName": "Me",
                            "platformMessageId": "a",
                        },
                        {
                            "localId": 138,
                            "createTime": 1778840038,
                            "formattedTime": "2026-05-15 18:13:58",
                            "type": "文本消息",
                            "content": "\n兑",
                            "isSend": 0,
                            "senderDisplayName": "学校-25-AI自强-肖逸涵",
                            "platformMessageId": "b",
                        },
                        {
                            "localId": 139,
                            "createTime": 1778840080,
                            "formattedTime": "2026-05-15 18:14:40",
                            "type": "文本消息",
                            "content": "\n拱猪教\n公主教如果从南门进来是三楼，要下一层到二楼",
                            "compressed_local_ids": [139, 140],
                            "isSend": 0,
                            "senderDisplayName": "学校-25-AI自强-巩子茗",
                            "platformMessageId": "c",
                        },
                        {
                            "localId": 141,
                            "createTime": 1778840115,
                            "formattedTime": "2026-05-15 18:15:15",
                            "type": "动画表情",
                            "content": "[表情]",
                            "isSend": 0,
                            "senderDisplayName": "学校-25-AI自强-肖逸涵",
                            "replyContext": {
                                "senderDisplayName": "学校-25-AI自强-巩子茗",
                                "content": "\n拱猪教",
                            },
                        },
                        {
                            "localId": 142,
                            "createTime": 1778840120,
                            "formattedTime": "2026-05-15 18:15:20",
                            "type": "文本消息",
                            "content": "确实[引用 小拱猪：拱猪教]",
                            "isSend": 0,
                            "senderDisplayName": "学校-25-AI自强-肖逸涵",
                            "replyContext": {
                                "senderDisplayName": "学校-25-AI自强-巩子茗",
                                "content": "\n拱猪教",
                            },
                        },
                        {
                            "localId": 143,
                            "createTime": 1778840128,
                            "formattedTime": "2026-05-15 18:15:28",
                            "type": "图片消息",
                            "content": "media/images/a.jpg",
                            "image_ocr": ["上传实验报告", "选择文件"],
                            "isSend": 0,
                            "senderDisplayName": "学校-25-AI自强-方明哲",
                        },
                        {
                            "localId": 144,
                            "createTime": 1778840130,
                            "formattedTime": "2026-05-15 18:15:30",
                            "type": "文本消息",
                            "content": "即日起本群群员必须信仰拱猪教",
                            "isSend": 0,
                            "senderDisplayName": "学校-25-AI自强-席圣洋",
                            "is_chatroom_top_message": True,
                        },
                        {
                            "localId": 145,
                            "createTime": 1778840135,
                            "formattedTime": "2026-05-15 18:15:35",
                            "type": "其他消息",
                            "content": '"肖逸涵" 拍了拍 "Me"',
                            "isSend": 0,
                            "senderDisplayName": "小团体",
                            "is_self_related_pat": True,
                        },
                    ],
                },
            )

            archive([export], output_root=out_root)
            text = (out_root / "Chat" / "2026-05-15.md").read_text(encoding="utf-8")

        self.assertIn("2026-05-15 18:13:23", text)
        self.assertIn("我：公主教就是工学部教1吗？", text)
        self.assertIn("肖逸涵：兑", text)
        self.assertIn("巩子茗：拱猪教 | 公主教如果从南门进来是三楼，要下一层到二楼", text)
        self.assertIn("肖逸涵：[表情][引用 巩子茗：拱猪教]", text)
        self.assertIn("肖逸涵：确实[引用 巩子茗：拱猪教]", text)
        self.assertNotIn("小拱猪", text)
        self.assertIn("方明哲：[图片：上传实验报告\n选择文件]", text)
        self.assertIn("席圣洋【置顶消息】：即日起本群群员必须信仰拱猪教", text)
        self.assertIn('拍一拍："肖逸涵" 拍了拍 "Me"', text)


if __name__ == "__main__":
    unittest.main()
