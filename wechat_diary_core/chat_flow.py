from __future__ import annotations

from datetime import datetime
from typing import Any
import html
import re


Message = dict[str, Any]
TIME_GAP_SECONDS = 5 * 60
TIME_MARK_SECONDS = 20 * 60
QUOTE_IMAGE_MAX_CHARS = 10
TRAILING_NOTE_RE = re.compile(r"(?:（[^（）]*）|\([^()]*\))+$")
NAME_SPLIT_RE = re.compile(r"[-－—–]")
VOICE_FAIL_PATTERN_RE = re.compile(r"^\[?语音消息\s*-\s*转文字失败.*\]?$")
XML_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)


def render_chat_flow(messages: list[Message]) -> str:
    lines: list[str] = []
    previous_time: int | None = None
    last_mark_time: int | None = None

    for message in messages:
        current_time = _message_time(message)
        if _needs_time_marker(current_time, previous_time, last_mark_time):
            lines.append(_message_time_text(message))
            last_mark_time = current_time

        sender = display_name_for_message(message)
        if message.get("is_chatroom_top_message"):
            sender = f"{sender}【置顶消息】"
        elif message.get("is_self_related_pat"):
            sender = "拍一拍"
        content = render_message_content(message)
        if content:
            lines.append(f"{sender}：{content}")

        previous_time = current_time

    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def display_name_for_message(message: Message) -> str:
    if int(message.get("isSend") or 0) == 1:
        return "我"
    return simplify_display_name(str(message.get("senderDisplayName") or message.get("senderRemark") or message.get("senderNickname") or message.get("senderUsername") or "未知"))


def simplify_display_name(name: str) -> str:
    original = _compact_spaces(name)
    if not original:
        return "未知"

    parts = NAME_SPLIT_RE.split(original)
    candidate = parts[-1].strip() if parts else original
    candidate = TRAILING_NOTE_RE.sub("", candidate).strip()
    return candidate or original


def render_message_content(message: Message) -> str:
    segments = message.get("compressed_segments")
    if isinstance(segments, list) and segments:
        parts = [render_message_content(segment) for segment in segments if isinstance(segment, dict)]
        return " | ".join(part for part in parts if part)

    content = _base_message_content(message)
    quote = _quote_content(message)
    if quote:
        content = f"{content}{quote}" if content else quote
    return content


def _base_message_content(message: Message) -> str:
    message_type = str(message.get("type") or "")
    raw_content = str(message.get("content") or "")

    if message_type == "动画表情" or raw_content.strip() in {"[表情包]", "[表情]"}:
        return "[表情]"

    if "图片" in message_type or message.get("image_ocr") or message.get("image_ocr_inline"):
        inline = _image_ocr_inline_for(message)
        return f"[图片：{inline}]" if inline else "[图片]"

    if message_type == "语音消息" and (message.get("transcribe_failed") or _is_voice_fail_only(raw_content)):
        return "[语音]"

    text = _compact_message_text(raw_content, join_lines=bool(message.get("compressed_local_ids")))
    if _has_quote_context(message):
        text = _strip_embedded_quote_text(text)
    return text


def _image_ocr_inline_for(message: Message) -> str:
    """Return the OCR text to render inside ``[图片：...]`` — pre-truncated by preprocessing."""
    inline = str(message.get("image_ocr_inline") or "").strip()
    if inline:
        return inline
    ocr_lines = [str(line).strip() for line in message.get("image_ocr") or [] if str(line).strip()]
    if ocr_lines:
        return "\n".join(ocr_lines)
    fallback = _extract_ocr_text(str(message.get("content") or ""))
    return fallback


def _is_voice_fail_only(raw_content: str) -> bool:
    return bool(VOICE_FAIL_PATTERN_RE.match(raw_content.strip()))


def _quote_content(message: Message) -> str:
    target = message.get("replyContext")
    if isinstance(target, dict):
        sender = "我" if int(target.get("isSend") or 0) == 1 else simplify_display_name(
            str(target.get("senderDisplayName") or target.get("senderUsername") or "未知")
        )
        text = _quote_text_for_target(target)
    else:
        if not any(message.get(field) for field in ("replyToMessageId", "quotedContent", "quotedSenderDisplayName", "quotedSender")):
            return ""
        sender = "我" if message.get("quotedIsSelf") else simplify_display_name(
            str(message.get("quotedSenderDisplayName") or message.get("quotedSender") or "未知")
        )
        text = _quote_text_from_raw(str(message.get("quotedContent") or ""))

    if not text:
        text = "[消息]"
    return f"[引用 {sender}：{text}]"


def _quote_text_for_target(target: Message) -> str:
    """Render a reply target's content with image / voice noise stripped."""
    if _target_is_image(target):
        ocr = _image_ocr_inline_for(target).replace("\n", "")
        if not ocr:
            return "[图片]"
        if len(ocr) > QUOTE_IMAGE_MAX_CHARS:
            return f"[图片：{ocr[:QUOTE_IMAGE_MAX_CHARS]}…]"
        return f"[图片：{ocr}]"

    if str(target.get("type") or "") == "语音消息" and (
        target.get("transcribe_failed") or _is_voice_fail_only(str(target.get("content") or ""))
    ):
        return "[语音]"
    if _has_quote_context(target):
        return render_message_content(target)

    raw = str(target.get("content") or target.get("quotedContent") or "")
    text = _quote_text_from_raw(raw)
    if _looks_like_wechat_xml(text):
        text = _strip_embedded_quote_text(text)
    return text


def _target_is_image(target: Message) -> bool:
    if target.get("image_ocr") or target.get("image_ocr_inline"):
        return True
    if "图片" in str(target.get("type") or ""):
        return True
    content = str(target.get("content") or "")
    return "media/images/" in content.replace("\\", "/") or "[OCR]" in content


def _has_quote_context(message: Message) -> bool:
    return any(message.get(field) for field in ("replyContext", "replyToMessageId", "quotedContent", "quotedSenderDisplayName", "quotedSender"))


def _compact_message_text(value: str, join_lines: bool = True) -> str:
    lines = [line.strip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    return " | ".join(lines) if join_lines else lines[0] if len(lines) == 1 else " | ".join(lines)


def _strip_embedded_quote_text(value: str) -> str:
    marker = "[引用 "
    index = value.find(marker)
    if index < 0:
        return value
    return value[:index].rstrip()


def _quote_text_from_raw(value: str) -> str:
    text = _compact_message_text(value, join_lines=True)
    unescaped = html.unescape(text)
    if not _looks_like_wechat_xml(unescaped):
        return text
    if unescaped.lstrip().startswith("<"):
        return _extract_xml_title(unescaped) or "[消息]"
    return _strip_embedded_quote_text(unescaped)


def _looks_like_wechat_xml(value: str) -> bool:
    return "<msg" in value or "<appmsg" in value or "&lt;msg" in value or "&lt;appmsg" in value


def _extract_xml_title(value: str) -> str:
    match = XML_TITLE_RE.search(html.unescape(value))
    return _compact_message_text(match.group(1), join_lines=True) if match else ""


def _extract_ocr_text(value: str) -> str:
    marker = "[OCR]"
    if marker not in value:
        return ""
    return value.split(marker, 1)[1].strip()


def _compact_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _needs_time_marker(current_time: int, previous_time: int | None, last_mark_time: int | None) -> bool:
    if previous_time is None or last_mark_time is None:
        return True
    if current_time - previous_time >= TIME_GAP_SECONDS:
        return True
    return current_time - last_mark_time >= TIME_MARK_SECONDS


def _message_time(message: Message) -> int:
    try:
        return int(message.get("createTime") or 0)
    except (TypeError, ValueError):
        return 0


def _message_time_text(message: Message) -> str:
    formatted = str(message.get("formattedTime") or "")
    if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", formatted):
        return formatted[:19]
    timestamp = _message_time(message)
    if timestamp:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    return formatted or "未知时间"
