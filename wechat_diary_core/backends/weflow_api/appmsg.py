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
