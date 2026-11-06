"""One safe textual representation for named stickers across consumers."""
from __future__ import annotations

from collections.abc import Mapping
import re
import unicodedata


MAX_EMOJI_NAME_CHARS = 80


def render_emoji(message: Mapping[str, object]) -> str:
    value = message.get("emojiName")
    if not isinstance(value, str):
        return "[表情]"
    # A name is one segment: neither line breaks nor pipes may create new ones.
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in value)
    text = re.sub(r"\s+", " ", text).strip().translate(str.maketrans({"|": "｜", "〔": "（", "〕": "）"}))
    if not text:
        return "[表情]"
    if len(text) > MAX_EMOJI_NAME_CHARS:
        text = text[:MAX_EMOJI_NAME_CHARS - 1] + "…"
    return f"〔{text}〕"
