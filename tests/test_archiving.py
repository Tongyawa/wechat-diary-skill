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


    def test_archive_clears_processed_root_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_root = Path(tmp) / "processed"
            stale = out_root / "私聊_old" / "2025-12-31.md"
            stale.parent.mkdir(parents=True)
            stale.write_text("stale", encoding="utf-8")
            export = ProcessedChatExport(
                source_path=Path(tmp) / "Chat_20260516" / "Chat_20260516.json",
                source_folder="Chat_20260516",
                data={
                    "session": {"type": "私聊", "messageCount": 1},
                    "messages": [{"formattedTime": "2026-05-16 10:00:00", "content": "hi"}],
                },
            )

            archive([export], output_root=out_root)

            self.assertFalse(stale.exists())
            self.assertTrue((out_root / "Chat" / "2026-05-16.md").exists())

    def test_archive_keeps_existing_files_when_clear_first_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_root = Path(tmp) / "processed"
            existing = out_root / "私聊_keep" / "2025-12-31.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("keep", encoding="utf-8")
            export = ProcessedChatExport(
                source_path=Path(tmp) / "Chat_20260516" / "Chat_20260516.json",
                source_folder="Chat_20260516",
                data={
                    "session": {"type": "私聊", "messageCount": 1},
                    "messages": [{"formattedTime": "2026-05-16 10:00:00", "content": "hi"}],
                },
            )

            archive([export], output_root=out_root, clear_first=False)

            self.assertTrue(existing.exists())
            self.assertTrue((out_root / "Chat" / "2026-05-16.md").exists())

    def test_quote_context_image_truncates_to_ten_chars(self) -> None:
        from wechat_diary_core.chat_flow import render_chat_flow

        long_ocr = "北京香壶ABCDEFGHIJ"
        messages = [
            {
                "createTime": 1778840000,
                "formattedTime": "2026-05-15 18:00:00",
                "type": "图片消息",
                "content": "media/images/x.jpg\n[OCR] " + long_ocr,
                "image_ocr": [long_ocr],
                "image_ocr_inline": long_ocr,
                "isSend": 0,
                "senderDisplayName": "Huuu.",
                "platformMessageId": "img-1",
            },
            {
                "createTime": 1778840060,
                "formattedTime": "2026-05-15 18:01:00",
                "type": "引用消息",
                "content": "回复瓜子脸",
                "isSend": 0,
                "senderDisplayName": "Bystander",
                "replyContext": {
                    "senderDisplayName": "Huuu.",
                    "type": "图片消息",
                    "content": "media/images/x.jpg\n[OCR] " + long_ocr,
                    "image_ocr": [long_ocr],
                    "image_ocr_inline": long_ocr,
                },
            },
        ]

        text = render_chat_flow(messages)

        self.assertIn("Huuu.：[图片：北京香壶ABCDEFGHIJ]", text)
        self.assertIn("Bystander：回复瓜子脸[引用 Huuu.：[图片：北京香壶ABCDEF…]]", text)
        self.assertNotIn("media/images/", text)

    def test_voice_fail_messages_collapse_to_voice_marker(self) -> None:
        from wechat_diary_core.chat_flow import render_chat_flow

        messages = [
            {
                "createTime": 1778840000,
                "formattedTime": "2026-05-15 18:00:00",
                "type": "语音消息",
                "content": "[语音消息 - 转文字失败: Silk 解码失败]",
                "transcribe_failed": True,
                "isSend": 0,
                "senderDisplayName": "Voicer",
            },
            {
                "createTime": 1778840060,
                "formattedTime": "2026-05-15 18:01:00",
                "type": "语音消息",
                "content": "晚安呢",
                "isSend": 0,
                "senderDisplayName": "Voicer",
            },
        ]

        text = render_chat_flow(messages)

        self.assertIn("Voicer：[语音]", text)
        self.assertIn("Voicer：晚安呢", text)
        self.assertNotIn("转文字失败", text)

    def test_nested_quote_appmsg_xml_is_rendered_from_resolved_context(self) -> None:
        from wechat_diary_core.chat_flow import render_chat_flow
        from wechat_diary_core.preprocessing.cleaner import clean_messages, resolve_reply_context
        from wechat_diary_core.config import load_config

        xml_quote = '<msg><appmsg appid="" sdkver="0"><title>nested quote body</title><type>57</type><content>'
        messages = [
            {
                "localId": 1,
                "createTime": 1778941200,
                "formattedTime": "2026-05-16 22:20:00",
                "type": "语音消息",
                "content": "[语音消息 - 转文字失败: Silk 解码失败]",
                "isSend": 0,
                "senderUsername": "other",
                "senderDisplayName": "Peer",
                "platformMessageId": "voice-1",
            },
            {
                "localId": 2,
                "createTime": 1778941299,
                "formattedTime": "2026-05-16 22:21:39",
                "type": "引用消息",
                "content": "nested quote body[引用 Peer：[语音]]",
                "isSend": 1,
                "senderUsername": "me",
                "senderDisplayName": "SelfRawName",
                "platformMessageId": "quote-1",
                "replyToMessageId": "voice-1",
                "quotedSender": "Peer",
                "quotedContent": "[语音]",
            },
            {
                "localId": 3,
                "createTime": 1778941543,
                "formattedTime": "2026-05-16 22:25:43",
                "type": "引用消息",
                "content": f"current message body[引用 SelfRawName：{xml_quote}]",
                "isSend": 1,
                "senderUsername": "me",
                "senderDisplayName": "SelfRawName",
                "platformMessageId": "quote-2",
                "replyToMessageId": "quote-1",
                "quotedSender": "SelfRawName",
                "quotedContent": xml_quote,
            },
            {
                "localId": 4,
                "createTime": 1778941571,
                "formattedTime": "2026-05-16 22:26:11",
                "type": "语音消息",
                "content": "[引用 [消息]]",
                "isSend": 0,
                "senderUsername": "other",
                "senderDisplayName": "Peer",
                "platformMessageId": "quote-3",
                "replyToMessageId": "quote-2",
                "quotedContent": "[消息]",
            },
        ]

        cfg = load_config(Path("missing.toml"))
        resolved = resolve_reply_context(clean_messages(messages, cfg.preprocessing))
        text = render_chat_flow(resolved)

        self.assertIn("我：current message body[引用 我：nested quote body[引用 Peer：[语音]]]", text)
        self.assertIn("Peer：[引用 我：current message body[引用 我：nested quote body[引用 Peer：[语音]]]]", text)
        self.assertNotIn("<msg", text)
        self.assertNotIn("SelfRawName", text)

    def test_compressed_quote_segment_preserves_reply_context_and_self_sender(self) -> None:
        from wechat_diary_core.chat_flow import render_chat_flow
        from wechat_diary_core.preprocessing.cleaner import clean_messages, resolve_reply_context
        from wechat_diary_core.preprocessing.time_compress import compress_nearby_messages
        from wechat_diary_core.config import load_config

        messages = [
            {
                "localId": 1,
                "createTime": 1778941434,
                "formattedTime": "2026-05-16 22:23:54",
                "type": "文本消息",
                "content": "self original text",
                "isSend": 1,
                "senderUsername": "me",
                "senderDisplayName": "SelfRawName",
                "platformMessageId": "self-1",
            },
            {
                "localId": 2,
                "createTime": 1778941545,
                "formattedTime": "2026-05-16 22:25:45",
                "type": "文本消息",
                "content": "first peer text",
                "isSend": 0,
                "senderUsername": "other",
                "senderDisplayName": "Peer",
                "platformMessageId": "other-1",
            },
            {
                "localId": 3,
                "createTime": 1778941549,
                "formattedTime": "2026-05-16 22:25:49",
                "type": "文本消息",
                "content": "second peer text",
                "isSend": 0,
                "senderUsername": "other",
                "senderDisplayName": "Peer",
                "platformMessageId": "other-2",
            },
            {
                "localId": 4,
                "createTime": 1778941560,
                "formattedTime": "2026-05-16 22:26:00",
                "type": "引用消息",
                "content": "third peer text[引用 SelfRawName：self original text]",
                "isSend": 0,
                "senderUsername": "other",
                "senderDisplayName": "Peer",
                "platformMessageId": "other-3",
                "replyToMessageId": "self-1",
                "quotedSender": "SelfRawName",
                "quotedContent": "self original text",
            },
        ]

        cfg = load_config(Path("missing.toml"))
        resolved = resolve_reply_context(clean_messages(messages, cfg.preprocessing))
        compressed = compress_nearby_messages(resolved, max_interval_sec=120)
        text = render_chat_flow(compressed)

        self.assertIn("Peer：first peer text | second peer text | third peer text[引用 我：self original text]", text)
        self.assertNotIn("SelfRawName", text)


if __name__ == "__main__":
    unittest.main()
