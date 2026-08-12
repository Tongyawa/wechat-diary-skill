from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from wechat_diary_core.backends.weflow_api.type_map import APP_TYPES, BASE_TYPES
from wechat_diary_core.chat_flow import render_message_content


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "tests" / "fixtures" / "processed_placeholder_catalog.json"
TAG_RE = re.compile(r"\[([^\]：]+)(?:：[^\]]*)?\]")
MAX_REPORTED_TAGS = 20


def _catalog() -> dict[str, list[str]]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _tag_from(rendered: str) -> str | None:
    match = TAG_RE.search(rendered)
    return match.group(1) if match else None


def _current_renderer_tags() -> set[str]:
    tags: set[str] = set()
    for message_type in (*BASE_TYPES.values(), *APP_TYPES.values()):
        rendered = render_message_content(
            {"type": message_type.canonical, "content": message_type.placeholder}
        )
        tag = _tag_from(rendered)
        if tag:
            tags.add(tag)
    tags.add(_tag_from(render_message_content({"type": "图片消息", "image_ocr_inline": "文本"})) or "")
    tags.add(_tag_from(render_message_content({"type": "语音消息", "transcribe_failed": True})) or "")
    return tags - {""}


class ProcessedFormatContractTests(unittest.TestCase):
    def test_real_archive_catalog_and_current_renderer_tags_are_documented(self) -> None:
        """真机反推的形态和当前 renderer 标签都必须在唯一格式契约中有说明。"""
        catalog = _catalog()
        document = (ROOT / "references" / "processed-format.md").read_text(
            encoding="utf-8"
        )
        expected_tags = set(catalog["observed_tags"]) | _current_renderer_tags()
        missing_tags = sorted(
            tag
            for tag in expected_tags
            if f"[{tag}]" not in document and f"[{tag}：" not in document
        )
        missing_structures = sorted(
            shape
            for shape in catalog["observed_structures"]
            if (
                shape == "[<具名表情>]"
                and "具名微信表情" not in document
            )
            or (
                shape != "[<具名表情>]"
                and shape.replace("<被引内容>", "<被引原文>") not in document
            )
        )

        # 归档标签 + 当前 type map 至多约 30 个；只显示前 20 个，确保全量
        # 漂移时仍能直接定位先修什么，而不是输出被消息正文放大的日志。
        shown_tags = missing_tags[:MAX_REPORTED_TAGS]
        suffix = (
            f"；另有 {len(missing_tags) - len(shown_tags)} 个标签未展示"
            if len(missing_tags) > len(shown_tags)
            else ""
        )
        self.assertFalse(
            missing_tags or missing_structures,
            "processed-format.md 未覆盖实际 renderer/真机归档占位："
            f"缺标签={shown_tags}{suffix}；缺形态={missing_structures}。"
            "请在对应消息类别补上实际方括号标签与带正文变体。",
        )

    def test_renderer_exercises_special_documented_shapes(self) -> None:
        """避免目录只验证静态标签，而没有走过图片、引用和具名表情的实际渲染。"""
        rendered = {
            "图片": render_message_content(
                {"type": "图片消息", "image_ocr_inline": "识别文本"}
            ),
            "引用": render_message_content(
                {
                    "type": "文本消息",
                    "content": "回复正文",
                    "replyContext": {
                        "isSend": 0,
                        "senderDisplayName": "发送者",
                        "content": "被引原文",
                        "type": "文本消息",
                    },
                }
            ),
            "具名表情": render_message_content(
                {"type": "文本消息", "content": "[中性表情]"}
            ),
        }
        self.assertEqual("[图片：识别文本]", rendered["图片"])
        self.assertEqual("回复正文[引用 发送者：被引原文]", rendered["引用"])
        self.assertEqual("[中性表情]", rendered["具名表情"])


if __name__ == "__main__":
    unittest.main()
