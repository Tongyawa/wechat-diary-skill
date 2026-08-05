from __future__ import annotations

import unittest

from wechat_diary_core.backends.weflow_api.type_map import (
    CHATLAB_TYPE_ALIGNMENT,
    resolve_message_type,
    unpack_local_type,
)


class WeflowApiTypeMapTests(unittest.TestCase):
    def test_verified_default_local_types(self) -> None:
        expected = {
            1: "文本消息",
            3: "图片消息",
            34: "语音消息",
            47: "动画表情",
            10000: "其他消息",
            21474836529: "其他消息",
            25769803825: "其他消息",
            81604378673: "其他消息",
            244813135921: "引用消息",
            266287972401: "其他消息",
            270582939697: "其他消息",
        }
        self.assertEqual(
            {value: resolve_message_type(value).canonical for value in expected},
            expected,
        )

    def test_packed_type_formula_and_verified_chatlab_alignment(self) -> None:
        self.assertEqual(unpack_local_type(244813135921), (49, 57))
        self.assertEqual(
            CHATLAB_TYPE_ALIGNMENT,
            {
                0: (1, "文本消息"),
                5: (47, "动画表情"),
                25: (244813135921, "引用消息"),
            },
        )


if __name__ == "__main__":
    unittest.main()
