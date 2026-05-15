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


if __name__ == "__main__":
    unittest.main()
