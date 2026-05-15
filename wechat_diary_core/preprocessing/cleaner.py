from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import copy
import json
import logging
import re

from ..config import Config, PreprocessingConfig, load_config
from .context_window import filter_group_context_window
from .exceptions import InvalidExportError
from .image_ocr import OcrEngine, annotate_image_messages
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
) -> ProcessedChatExport:
    cfg = config or load_config()
    path = Path(source_path)
    data = load_chat_export(path)
    messages = list(data.get("messages") or [])
    messages = clean_messages(messages, cfg.preprocessing)

    session_type = str(data.get("session", {}).get("type") or "")
    if session_type == "群聊":
        window = cfg.preprocessing.group_context_window
        messages = filter_group_context_window(
            messages,
            messages_before=window.messages_before,
            messages_after=window.messages_after,
            time_window_minutes=window.time_window_minutes,
        )

    messages = annotate_image_messages(messages, path.parent, cfg.preprocessing, engine=ocr_engine)
    messages = compress_nearby_messages(messages, cfg.preprocessing.time_compress_interval_sec)

    processed = copy.deepcopy(data)
    processed["messages"] = messages
    processed.setdefault("session", {})["messageCount"] = len(messages)
    return ProcessedChatExport(source_path=path, source_folder=path.parent.name, data=processed)


def load_chat_export(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)

    if not isinstance(data, dict) or not isinstance(data.get("session"), dict) or not isinstance(data.get("messages"), list):
        raise InvalidExportError(f"Not a chat export: {path}")
    return data


def clean_messages(messages: list[Message], settings: PreprocessingConfig) -> list[Message]:
    cleaned: list[Message] = []
    for message in messages:
        current = copy.deepcopy(message)
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
