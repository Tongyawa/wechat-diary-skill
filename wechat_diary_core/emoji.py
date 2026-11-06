"""One safe textual representation for named stickers across consumers."""
from __future__ import annotations

from collections.abc import Mapping
import json
import logging
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qs, urlsplit


MAX_EMOJI_NAME_CHARS = 80
LOGGER = logging.getLogger(__name__)
_MD5 = re.compile(r"[0-9a-f]{32}")
_CAS = re.compile(r"(?:^|/)\_media/emojis/([0-9a-f]{2})/([0-9a-f]{32})\.[a-zA-Z0-9]+$")


def emoji_md5(message: Mapping[str, object]) -> str | None:
    """Use the observed CAS reference or legacy CDN query, never display text."""
    for key in ("source", "content"):
        value = message.get(key)
        if isinstance(value, str):
            match = _CAS.search(value.replace("\\", "/"))
            if match and match[1] == match[2][:2]:
                return match[2]
    value = message.get("emojiCdnUrl")
    if isinstance(value, str):
        try:
            values = parse_qs(urlsplit(value).query).get("m", [])
            if len(values) == 1 and _MD5.fullmatch(values[0]):
                return values[0]
        except ValueError:
            pass
    return None


class EmojiLabels:
    """One read-only snapshot per configuration, shared by all chat files."""

    def __init__(self, path: Path | None):
        self.path = path
        self.labels: dict[str, dict[str, str]] = {}
        self.problem = ""
        self._warned = False
        if path is None:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
                self.problem = "标签表版本不认识（只支持 version=1）"
                return
            if not isinstance(data.get("labels"), dict):
                self.problem = "labels 必须是对象"
                return
            ignored = 0
            normalized = 0
            for md5, entry in data["labels"].items():
                if not _MD5.fullmatch(md5) or not isinstance(entry, dict):
                    ignored += 1
                    continue
                if any(entry.get(key) is not None and not isinstance(entry[key], str)
                       for key in ("name", "album", "text")):
                    ignored += 1
                    continue
                label = {key: _display_text(entry.get(key)) for key in ("name", "album", "text")}
                normalized += int(any(isinstance(entry.get(key), str) and
                                      (label[key] != entry[key] or len(label[key]) > MAX_EMOJI_NAME_CHARS)
                                      for key in label))
                self.labels[md5] = label
            if ignored or normalized:
                self.problem = f"忽略 {ignored} 条坏条目，安全归一或截断 {normalized} 条显示内容"
        except (OSError, ValueError, RecursionError) as exc:
            # Include no entry content or exception payload (JSON can contain private text).
            self.problem = f"标签表无法读取或 JSON 损坏（{type(exc).__name__}）"

    def warn_once(self) -> None:
        if self.problem and not self._warned:
            self._warned = True
            LOGGER.warning("[WARN] 表情标签表：%s；路径：%s。导出继续，未命中条目沿用原表情显示；请检查 [emoji_labels].path。",
                           self.problem, self.path)

    def lookup(self, message: Mapping[str, object]) -> dict[str, str] | None:
        self.warn_once()
        return self.labels.get(emoji_md5(message) or "")


def _display_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    # A name is one segment: neither line breaks nor pipes may create new ones.
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in value)
    text = re.sub(r"\s+", " ", text).strip().translate(str.maketrans({"|": "｜", "〔": "（", "〕": "）"}))
    return text


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def render_emoji(message: Mapping[str, object]) -> str:
    # _emoji_label exists only on preprocessing copies, never canonical raw.
    label = message.get("_emoji_label")
    label = label if isinstance(label, Mapping) else {}
    text = (_display_text(label.get("text")) or _display_text(message.get("emojiName"))
            or _display_text(label.get("name")))
    album = _display_text(label.get("album"))
    if not text:
        text, album = album, ""
    if not text:
        return "[表情]"
    if album and len(text) + 2 <= MAX_EMOJI_NAME_CHARS:
        # Preserve the subject; the album is the first part shortened or dropped.
        text += "·" + _truncate(album, MAX_EMOJI_NAME_CHARS - len(text) - 1)
    if len(text) > MAX_EMOJI_NAME_CHARS:
        text = _truncate(text, MAX_EMOJI_NAME_CHARS)
    return f"〔{text}〕"
