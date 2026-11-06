"""Read the observed emoji desc protobuf without retaining unrelated XML data.

Wire shape is inferred from real API samples, not an upstream schema promise.
emojiattr is deliberately ignored: its meaning has not been established.
"""
from __future__ import annotations

import base64
import binascii
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator


MAX_DESC_CHARS = 8192
MAX_XML_CHARS = 65536
LANGUAGE_PRIORITY = ("zh_cn", "default", "zh_tw")


def _varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if position >= len(data):
            raise ValueError("truncated varint")
        byte = data[position]
        position += 1
        if shift == 63 and byte > 1:
            raise ValueError("varint overflow")
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, position
    raise ValueError("varint overflow")


def _fields(data: bytes) -> Iterator[tuple[int, bytes | int]]:
    position = 0
    while position < len(data):
        tag, position = _varint(data, position)
        field, wire = tag >> 3, tag & 7
        if not 0 < field < (1 << 29):
            raise ValueError("invalid field number")
        if wire == 0:
            value, position = _varint(data, position)
            yield field, value
            continue
        if wire == 2:
            length, position = _varint(data, position)
        elif wire in (1, 5):
            length = 8 if wire == 1 else 4
        else:
            raise ValueError("unsupported wire type")
        end = position + length
        if end > len(data):
            raise ValueError("truncated field")
        if wire == 2:
            yield field, data[position:end]
        position = end


def decode_desc(value: str) -> str:
    """Choose nonblank zh_cn > default > zh_tw > first other language.

    Reject the whole malformed payload, including truncated trailing fields.
    Within a language, the first nonblank entry wins deterministically.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_DESC_CHARS:
        return ""
    try:
        captions: dict[str, str] = {}
        for field, entry in _fields(base64.b64decode(value, validate=True)):
            if field != 1 or not isinstance(entry, bytes):
                continue
            language = text = ""
            for key, part in _fields(entry):
                if key in (1, 2) and isinstance(part, bytes):
                    decoded = part.decode("utf-8")
                    if key == 1:
                        language = decoded
                    else:
                        text = decoded
            if language and text.strip() and not captions.get(language):
                captions[language] = text
        for language in LANGUAGE_PRIORITY:
            if captions.get(language):
                return captions[language]
        return next(iter(captions.values()), "")
    except (ValueError, binascii.Error, UnicodeError):
        return ""


def emoji_metadata(raw_content: object) -> dict[str, str]:
    """Return bounded original desc and optional decoded name for type 47."""
    if not isinstance(raw_content, str) or len(raw_content) > MAX_XML_CHARS:
        return {}
    payload = re.sub(r"^[^\s<>:]+:\r?\n", "", raw_content.strip(), count=1).lstrip()
    if "<!DOCTYPE" in payload or "<!ENTITY" in payload:
        return {}
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, UnicodeError, ValueError):
        return {}
    emoji = root if root.tag == "emoji" else root.find(".//emoji")
    if emoji is None:
        return {}
    desc = emoji.get("desc") or ""
    if not desc or len(desc) > MAX_DESC_CHARS:
        return {}
    result = {"emojiDesc": desc}
    name = decode_desc(desc)
    if name:
        result["emojiName"] = name
    return result
