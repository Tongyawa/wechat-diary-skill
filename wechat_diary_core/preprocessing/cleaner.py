from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
from pathlib import Path
from typing import Any
import copy
import json
import logging
import re

from ..config import Config, PreprocessingConfig, load_config
from .context_window import filter_group_context_window
from .exceptions import InvalidExportError
from .image_ocr import ImageMode, OcrEngine, annotate_image_messages
from .time_compress import compress_nearby_messages


LOGGER = logging.getLogger(__name__)
Message = dict[str, Any]


@dataclass(frozen=True)
class ProcessedChatExport:
    source_path: Path
    source_folder: str
    data: dict[str, Any]


def preprocess_export(
    source_path: str | Path,
    config: Config | None = None,
    ocr_engine: OcrEngine | None = None,
    image_mode: ImageMode = "ocr_inline",
) -> ProcessedChatExport:
    cfg = config or load_config()
    path = Path(source_path)
    data = load_chat_export(path)
    messages = list(data.get("messages") or [])
    messages = clean_messages(
        messages,
        cfg.preprocessing,
        pat_keep_names=cfg.preprocessing.group_context_window.anchor_keywords,
    )

    session_type = str(data.get("session", {}).get("type") or "")
    if session_type == "群聊":
        window = cfg.preprocessing.group_context_window
        messages = filter_group_context_window(
            messages,
            messages_before=window.messages_before,
            messages_after=window.messages_after,
            time_window_minutes=window.time_window_minutes,
            self_wxids=cfg.user.self_wxids,
            anchor_keywords=window.anchor_keywords,
        )

    messages = annotate_image_messages(messages, path.parent, cfg.preprocessing, engine=ocr_engine, image_mode=image_mode)
    messages = resolve_reply_context(messages)
    messages = compress_nearby_messages(messages, cfg.preprocessing.time_compress_interval_sec)

    processed = copy.deepcopy(data)
    processed["messages"] = messages
    processed.setdefault("session", {})["messageCount"] = len(messages)
    return ProcessedChatExport(source_path=path, source_folder=path.parent.name, data=processed)


def resolve_reply_context(messages: list[Message]) -> list[Message]:
    by_platform_id = {
        str(message.get("platformMessageId")): message
        for message in messages
        if message.get("platformMessageId") is not None
    }
    resolved: list[Message] = []
    for message in messages:
        current = copy.deepcopy(message)
        reply_to = current.get("replyToMessageId")
        target = by_platform_id.get(str(reply_to)) if reply_to is not None else None
        if target:
            current["replyContext"] = {
                "senderUsername": target.get("senderUsername"),
                "senderDisplayName": target.get("senderDisplayName"),
                "isSend": target.get("isSend"),
                "content": target.get("content"),
                "type": target.get("type"),
                "platformMessageId": target.get("platformMessageId"),
                "image_ocr": target.get("image_ocr"),
                "image_ocr_inline": target.get("image_ocr_inline"),
            }
        resolved.append(current)
    return resolved


def load_chat_export(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)

    if not isinstance(data, dict) or not isinstance(data.get("session"), dict) or not isinstance(data.get("messages"), list):
        raise InvalidExportError(f"Not a chat export: {path}")
    return data


def clean_messages(
    messages: list[Message],
    settings: PreprocessingConfig,
    pat_keep_names: Sequence[str] | None = None,
) -> list[Message]:
    cleaned: list[Message] = []
    self_display_names = _self_display_names(messages, pat_keep_names or [])
    pending_chatroom_top_message = False
    for message in messages:
        if _is_chatroom_top_protocol(message):
            pending_chatroom_top_message = _is_chatroom_top_pin_request(message)
            continue
        is_self_related_pat = _is_self_related_pat_message(message, self_display_names)
        if _is_pat_message(message) and not is_self_related_pat:
            pending_chatroom_top_message = False
            continue
        current = copy.deepcopy(message)
        if is_self_related_pat:
            current["is_self_related_pat"] = True
        if pending_chatroom_top_message:
            current["is_chatroom_top_message"] = True
            pending_chatroom_top_message = False
        if settings.skip_emoji_dir and _is_emoji_message(current):
            current["content"] = "[表情]"
            if _is_emoji_ref(str(current.get("source") or "")):
                current["source"] = ""

        if _has_transcription_failure(current):
            current["transcribe_failed"] = True
            if settings.voice_fail_log_only:
                LOGGER.warning("Voice transcription failed in message %s.", current.get("localId"))

        cleaned.append(current)
    return cleaned


def discover_chat_exports(root: str | Path) -> list[Path]:
    path = Path(root)
    if path.is_file():
        return [path]
    if not path.exists():
        raise FileNotFoundError(path)

    return sorted(candidate for candidate in path.rglob("*.json") if _looks_like_chat_export(candidate))


def _looks_like_chat_export(path: Path) -> bool:
    try:
        load_chat_export(path)
    except (InvalidExportError, json.JSONDecodeError, OSError):
        return False
    return True


def _is_emoji_message(message: Message) -> bool:
    content = str(message.get("content") or "")
    source = str(message.get("source") or "")
    return (
        str(message.get("type") or "") == "动画表情"
        or content in {"[表情包]", "[表情]"}
        or _is_emoji_ref(content)
        or _is_emoji_ref(source)
    )


def _is_emoji_ref(value: str) -> bool:
    return bool(re.search(r"(^|[\\/])media[\\/]emojis[\\/]", value.replace("\\", "/")))


def _has_transcription_failure(message: Message) -> bool:
    haystack = f"{message.get('content') or ''}\n{message.get('source') or ''}"
    return "转文字失败" in haystack


def _is_chatroom_top_protocol(message: Message) -> bool:
    content = str(message.get("content") or "")
    return "<!-- ChatRoomTopMsgRequest -->" in content or "<!-- ChatRoomTopMsgResponse -->" in content


def _is_chatroom_top_pin_request(message: Message) -> bool:
    content = str(message.get("content") or "")
    match = re.search(r"<!-- ChatRoomTopMsgRequest -->\s+\S+@chatroom\s+(\d+)\s+", content)
    return bool(match and match.group(1) == "2")


def _is_pat_message(message: Message) -> bool:
    content = str(message.get("content") or "")
    return str(message.get("type") or "") == "其他消息" and " 拍了拍 " in content


def _is_self_related_pat_message(message: Message, self_display_names: set[str]) -> bool:
    if not _is_pat_message(message):
        return False
    if int(message.get("isSend") or 0) == 1:
        return True
    content = str(message.get("content") or "")
    return any(name and name in content for name in self_display_names)


def _self_display_names(messages: Sequence[Message], extra_names: Sequence[str]) -> set[str]:
    names = {str(name).strip() for name in extra_names if str(name).strip()}
    for message in messages:
        if int(message.get("isSend") or 0) != 1:
            continue
        for field in ("senderDisplayName", "senderRemark", "senderNickname"):
            value = str(message.get(field) or "").strip()
            if value:
                names.add(value)
    return names
