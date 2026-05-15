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
) -> list[Message]:
    if not messages:
        return []

    anchors = [index for index, message in enumerate(messages) if int(message.get("isSend", 0)) == 1]
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
