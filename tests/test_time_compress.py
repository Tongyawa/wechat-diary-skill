from __future__ import annotations

import unittest

from wechat_diary_core.preprocessing.time_compress import compress_nearby_messages


class TimeCompressTests(unittest.TestCase):
    def test_merges_adjacent_text_from_same_sender(self) -> None:
        messages = [
            {"localId": 1, "createTime": 1000, "type": "文本消息", "content": "a", "isSend": 1, "senderUsername": "me", "platformMessageId": "p1"},
            {"localId": 2, "createTime": 1050, "type": "文本消息", "content": "b", "isSend": 1, "senderUsername": "me", "platformMessageId": "p2"},
            {"localId": 3, "createTime": 1200, "type": "文本消息", "content": "c", "isSend": 0, "senderUsername": "other"},
        ]

        compressed = compress_nearby_messages(messages, max_interval_sec=120)

        self.assertEqual(len(compressed), 2)
        self.assertEqual(compressed[0]["createTime"], 1000)
        self.assertEqual(compressed[0]["content"], "a\nb")
        self.assertEqual(compressed[0]["compressed_local_ids"], [1, 2])
        self.assertEqual(compressed[0]["compressed_platform_message_ids"], ["p1", "p2"])


if __name__ == "__main__":
    unittest.main()
