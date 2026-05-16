from __future__ import annotations

import unittest

from wechat_diary_core.preprocessing.context_window import filter_group_context_window


class ContextWindowTests(unittest.TestCase):
    def test_no_anchor_drops_group_chat(self) -> None:
        messages = [{"createTime": 1000, "isSend": 0}, {"createTime": 1060, "isSend": 0}]

        self.assertEqual(filter_group_context_window(messages, 1, 1, 10), [])

    def test_count_and_time_windows_take_union(self) -> None:
        messages = [
            {"localId": 1, "createTime": 1000, "isSend": 0},
            {"localId": 2, "createTime": 1100, "isSend": 0},
            {"localId": 3, "createTime": 1200, "isSend": 1},
            {"localId": 4, "createTime": 1300, "isSend": 0},
            {"localId": 5, "createTime": 1800, "isSend": 0},
        ]

        kept = filter_group_context_window(messages, messages_before=0, messages_after=0, time_window_minutes=4)

        self.assertEqual([message["localId"] for message in kept], [1, 2, 3, 4])

    def test_keeps_message_quoted_by_self_even_when_far_away(self) -> None:
        messages = [
            {"localId": 1, "createTime": 1000, "isSend": 0, "platformMessageId": "p1"},
            {"localId": 2, "createTime": 5000, "isSend": 0, "platformMessageId": "p2"},
            {"localId": 3, "createTime": 9000, "isSend": 1, "replyToMessageId": "p1"},
        ]

        kept = filter_group_context_window(messages, messages_before=0, messages_after=0, time_window_minutes=1)

        self.assertEqual([message["localId"] for message in kept], [1, 3])

    def test_keeps_message_quoting_self_and_keyword_hits(self) -> None:
        messages = [
            {"localId": 1, "createTime": 1000, "isSend": 1, "platformMessageId": "self"},
            {"localId": 2, "createTime": 5000, "isSend": 0, "replyToMessageId": "self"},
            {"localId": 3, "createTime": 9000, "isSend": 0, "content": "please check alias"},
        ]

        kept = filter_group_context_window(messages, messages_before=0, messages_after=0, time_window_minutes=1, anchor_keywords=["alias"])

        self.assertEqual([message["localId"] for message in kept], [1, 2, 3])

    def test_self_related_pat_message_is_anchor(self) -> None:
        messages = [
            {"localId": 1, "createTime": 1000, "isSend": 0},
            {"localId": 2, "createTime": 5000, "isSend": 0, "is_self_related_pat": True},
        ]

        kept = filter_group_context_window(messages, messages_before=0, messages_after=0, time_window_minutes=1)

        self.assertEqual([message["localId"] for message in kept], [2])


if __name__ == "__main__":
    unittest.main()
