from __future__ import annotations

from collections.abc import Sequence
from typing import Any
import copy


Message = dict[str, Any]
MERGEABLE_TYPES = {"文本消息", "引用消息"}


def compress_nearby_messages(
    messages: Sequence[Message],
    max_interval_sec: int = 120,
    mergeable_types: set[str] | None = None,
) -> list[Message]:
    if not messages:
        return []

    merge_types = mergeable_types or MERGEABLE_TYPES
    compressed: list[Message] = []

    for message in messages:
        current = copy.deepcopy(message)
        if compressed and _can_merge(compressed[-1], current, max_interval_sec, merge_types):
            _merge_into(compressed[-1], current)
        else:
            compressed.append(current)

    return compressed


def _can_merge(left: Message, right: Message, max_interval_sec: int, merge_types: set[str]) -> bool:
    if left.get("type") not in merge_types or right.get("type") not in merge_types:
        return False
    if left.get("senderUsername") != right.get("senderUsername"):
        return False
    if int(left.get("isSend", 0)) != int(right.get("isSend", 0)):
        return False
    if not _content(left) or not _content(right):
        return False

    left_end_time = int(left.get("endCreateTime") or left.get("createTime") or 0)
    right_time = int(right.get("createTime") or 0)
    return 0 <= right_time - left_end_time <= max_interval_sec


def _merge_into(left: Message, right: Message) -> None:
    left["compressed_segments"] = _message_segments(left) + _message_segments(right)
    left["content"] = f"{_content(left)}\n{_content(right)}"
    left["endCreateTime"] = int(right.get("createTime") or left.get("endCreateTime") or left.get("createTime") or 0)
    left["compressed_count"] = int(left.get("compressed_count", 1)) + int(right.get("compressed_count", 1))

    local_ids = list(left.get("compressed_local_ids") or [left.get("localId")])
    right_ids = right.get("compressed_local_ids") or [right.get("localId")]
    left["compressed_local_ids"] = [item for item in local_ids + list(right_ids) if item is not None]

    platform_ids = list(left.get("compressed_platform_message_ids") or [left.get("platformMessageId")])
    right_platform_ids = right.get("compressed_platform_message_ids") or [right.get("platformMessageId")]
    left["compressed_platform_message_ids"] = [item for item in platform_ids + list(right_platform_ids) if item is not None]


def _content(message: Message) -> str:
    return str(message.get("content") or "").strip()


def _message_segments(message: Message) -> list[Message]:
    segments = message.get("compressed_segments")
    if isinstance(segments, list) and segments:
        return [copy.deepcopy(segment) for segment in segments if isinstance(segment, dict)]
    return [_segment_copy(message)]


def _segment_copy(message: Message) -> Message:
    segment = copy.deepcopy(message)
    for key in (
        "compressed_segments",
        "compressed_count",
        "compressed_local_ids",
        "compressed_platform_message_ids",
        "endCreateTime",
    ):
        segment.pop(key, None)
    return segment
