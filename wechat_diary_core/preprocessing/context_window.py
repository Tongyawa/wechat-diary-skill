from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from typing import Any
import copy


Message = dict[str, Any]


def filter_group_context_window(
    messages: Sequence[Message],
    messages_before: int,
    messages_after: int,
    time_window_minutes: int,
    self_wxids: Sequence[str] | None = None,
    anchor_keywords: Sequence[str] | None = None,
) -> list[Message]:
    if not messages:
        return []

    anchors = _anchor_indexes(messages, set(self_wxids or []), [item for item in anchor_keywords or [] if item])
    if not anchors:
        return []

    times = [_message_time(message) for message in messages]
    window_seconds = int(time_window_minutes * 60)
    intervals: list[tuple[int, int]] = []

    for index in anchors:
        anchor_time = times[index]
        count_start = max(0, index - messages_before)
        count_end = min(len(messages) - 1, index + messages_after)
        time_start = bisect_left(times, anchor_time - window_seconds)
        time_end = bisect_right(times, anchor_time + window_seconds) - 1
        start = min(count_start, time_start)
        end = max(count_end, time_end)
        intervals.append((start, end))

    return [copy.deepcopy(messages[index]) for start, end in _merge_intervals(intervals) for index in range(start, end + 1)]


def _anchor_indexes(messages: Sequence[Message], self_wxids: set[str], anchor_keywords: Sequence[str]) -> list[int]:
    by_platform_id = {
        str(message.get("platformMessageId")): index
        for index, message in enumerate(messages)
        if message.get("platformMessageId") is not None
    }
    anchors: set[int] = set()

    for index, message in enumerate(messages):
        if message.get("is_self_related_pat"):
            anchors.add(index)
            continue

        if _is_self_message(message, self_wxids):
            anchors.add(index)
            quoted_index = _quoted_index(message, by_platform_id)
            if quoted_index is not None:
                anchors.add(quoted_index)
            continue

        quoted_index = _quoted_index(message, by_platform_id)
        if quoted_index is not None and _is_self_message(messages[quoted_index], self_wxids):
            anchors.add(index)
            anchors.add(quoted_index)
            continue

        if _matches_anchor_keyword(message, anchor_keywords):
            anchors.add(index)

    return sorted(anchors)


def _is_self_message(message: Message, self_wxids: set[str]) -> bool:
    if int(message.get("isSend", 0)) == 1:
        return True
    username = str(message.get("senderUsername") or "")
    return bool(username and username in self_wxids)


def _quoted_index(message: Message, by_platform_id: dict[str, int]) -> int | None:
    reply_to = message.get("replyToMessageId")
    if reply_to is None:
        return None
    return by_platform_id.get(str(reply_to))


def _matches_anchor_keyword(message: Message, anchor_keywords: Sequence[str]) -> bool:
    if not anchor_keywords:
        return False
    haystack = "\n".join(
        str(message.get(field) or "")
        for field in ("content", "quotedContent", "source")
    )
    return any(keyword in haystack for keyword in anchor_keywords)


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []

    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
            continue
        prev_start, prev_end = merged[-1]
        merged[-1] = (prev_start, max(prev_end, end))
    return merged


def _message_time(message: Message) -> int:
    return int(message.get("createTime") or 0)
