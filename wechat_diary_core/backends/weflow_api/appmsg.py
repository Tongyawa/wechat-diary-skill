"""Parse metadata from WeFlow default-format app messages."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass


# 真机 A/B 实测：旧版微信会把不支持的 appmsg 标题/描述写成这些样板文案。
# 这里只用于决定字段是否等同空值；不对其他用户文本做替换。
UNSUPPORTED_APPMSG_TEXT_RE = re.compile(
    r"^(?:当前(?:微信)?版本不支持展示该内容，请升级至最新版本|"
    r"你的微信版本较低，不能接收外部红包，请升级微信)[。.]?$"
)


_INVALID_XML_CHAR_RE = re.compile(
    r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]"
)
_CHAR_REFERENCE_RE = re.compile(r"&#(?:([0-9]+)|x([0-9a-fA-F]+));")
# Consume quoted tag attributes as a whole. A declaration-looking string inside
# an attribute is not a processing instruction; CDATA/comments/ordinary PIs
# likewise contain literal text, not active character references/declarations.
_XML_MARKUP_RE = re.compile(
    r"<!\[CDATA\[.*?\]\]>|<!--.*?-->|<\?.*?\?>|<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>",
    re.DOTALL,
)
_XML_DECLARATION_RE = re.compile(r"<\?xml(?=[ \t\r\n]|\?>)")


def _is_xml_char(value: int) -> bool:
    return (value in {9, 10, 13} or 0x20 <= value <= 0xD7FF
            or 0xE000 <= value <= 0xFFFD or 0x10000 <= value <= 0x10FFFF)


def _clean_char_references(content: str) -> str:
    def replace(match: re.Match[str]) -> str:
        digits = (match[1] or match[2]).lstrip("0") or "0"
        base = 10 if match[1] is not None else 16
        # Bound conversion even for malformed untrusted XML with huge numbers.
        if len(digits) > (7 if base == 10 else 6):
            return ""
        return match[0] if _is_xml_char(int(digits, base)) else ""
    return _CHAR_REFERENCE_RE.sub(replace, content)


def _sanitize_xml(content: str) -> str:
    """Remove XML 1.0 forbidden characters/references and misplaced declarations."""
    content = _INVALID_XML_CHAR_RE.sub("", content)
    pieces: list[str] = []
    previous = 0
    for match in _XML_MARKUP_RE.finditer(content):
        pieces.append(_clean_char_references(content[previous:match.start()]))
        token = match[0]
        if _XML_DECLARATION_RE.match(token):
            # Keep a declaration at the document start (optionally after BOM).
            if match.start() == 0 or (match.start() == 1 and content[0] == "\ufeff"):
                pieces.append(token)
        elif token.startswith(("<![CDATA[", "<!--", "<?")):
            pieces.append(token)
        else:
            pieces.append(_clean_char_references(token))
        previous = match.end()
    pieces.append(_clean_char_references(content[previous:]))
    return "".join(pieces)


@dataclass(frozen=True)
class AppmsgMeta:
    title: str
    des: str
    fileext: str
    totallen: int | None


def _normalize_text(value: str, max_chars: int) -> str:
    value = re.sub(r"\s*\n\s*", " / ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if UNSUPPORTED_APPMSG_TEXT_RE.fullmatch(value):
        return ""
    max_chars = max(1, max_chars)
    if len(value) > max_chars:
        return value[:max_chars] + "…"
    return value


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def parse_appmsg(content: str, max_chars: int = 300) -> AppmsgMeta | None:
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, TypeError, UnicodeEncodeError, ValueError):
        # Legal XML takes the original path unchanged. Recovery never repairs
        # missing tags/entities or guesses message text from malformed markup.
        if not isinstance(content, str):
            return None
        try:
            root = ET.fromstring(_sanitize_xml(content))
        except (ET.ParseError, UnicodeEncodeError, ValueError):
            return None

    appmsg = root if root.tag == "appmsg" else root.find(".//appmsg")
    if appmsg is None:
        return None
    appattach = appmsg.find("appattach")
    total_text = _element_text(appattach.find("totallen") if appattach is not None else None)
    try:
        totallen = int(total_text) if total_text else None
    except ValueError:
        totallen = None
    return AppmsgMeta(
        title=_normalize_text(_element_text(appmsg.find("title")), max_chars),
        des=_normalize_text(_element_text(appmsg.find("des")), max_chars),
        fileext=_element_text(appattach.find("fileext") if appattach is not None else None),
        totallen=totallen,
    )


__all__ = ["AppmsgMeta", "UNSUPPORTED_APPMSG_TEXT_RE", "parse_appmsg"]
