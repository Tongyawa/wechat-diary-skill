"""Persistent, inspectable message-count plans for chunked bulk exports."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

from .bulk_state import DateInterval


PLAN_VERSION = 1


class BulkExportPlan:
    """Keep per-day, media-free counts separately from completed export state."""

    def __init__(self, path: Path, raw_root: Path, payload: dict[str, Any] | None = None) -> None:
        self.path = path.resolve()
        self.raw_root = raw_root.resolve()
        self.payload = payload or {
            "version": PLAN_VERSION,
            "outputRoot": str(self.raw_root),
            "sessions": {},
        }

    @classmethod
    def load(cls, path: Path, raw_root: Path) -> "BulkExportPlan":
        plan_path = path.resolve()
        resolved_root = raw_root.resolve()
        if not plan_path.exists():
            return cls(plan_path, resolved_root)
        try:
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"批量导出 plan 文件不可读，拒绝覆盖：{plan_path}（{exc}）") from exc
        if not isinstance(payload, dict) or payload.get("version") != PLAN_VERSION:
            raise ValueError(f"批量导出 plan 版本不受支持，拒绝覆盖：{plan_path}")
        recorded_root = Path(str(payload.get("outputRoot") or "")).resolve()
        if recorded_root != resolved_root:
            raise ValueError(
                "批量导出 plan 属于其他输出根，拒绝复用："
                f"plan={recorded_root}；本次={resolved_root}"
            )
        if not isinstance(payload.get("sessions"), dict):
            raise ValueError(f"批量导出 plan 缺少 sessions 对象，拒绝覆盖：{plan_path}")
        return cls(plan_path, resolved_root, payload)

    def uncovered(self, wxid: str, start: date, end: date) -> list[DateInterval]:
        covered: set[date] = set()
        for record in self._completed_records(wxid):
            record_start = _parse_day(record.get("start"))
            record_end = _parse_day(record.get("end"))
            if record_start is None or record_end is None or record_start > record_end:
                continue
            current = max(start, record_start)
            last = min(end, record_end)
            while current <= last:
                covered.add(current)
                current += timedelta(days=1)
        return _uncovered_intervals(start, end, covered)

    def invalidate(self, wxid: str, start: date, end: date) -> None:
        """Forget a requested range while preserving successful plan outside it."""

        session = self._session(wxid, "")
        daily_counts = session.get("dailyCounts", {})
        if isinstance(daily_counts, dict):
            for current in _days(start, end):
                daily_counts.pop(current.isoformat(), None)
        completed = session.get("completed", [])
        if isinstance(completed, list):
            replacement: list[dict[str, Any]] = []
            for record in completed:
                if not isinstance(record, dict):
                    continue
                record_start = _parse_day(record.get("start"))
                record_end = _parse_day(record.get("end"))
                if record_start is None or record_end is None:
                    continue
                if record_end < start or end < record_start:
                    replacement.append(record)
                    continue
                if record_start < start:
                    replacement.append(_interval_record(record_start, start - timedelta(days=1)))
                if end < record_end:
                    replacement.append(_interval_record(end + timedelta(days=1), record_end))
            session["completed"] = replacement
        session.pop("lastFailure", None)
        self.save()

    def record_completed(
        self,
        wxid: str,
        display_name: str,
        interval: DateInterval,
        daily_counts: Mapping[date, int],
    ) -> None:
        normalized = _normalize_daily_counts(interval, daily_counts)
        session = self._session(wxid, display_name)
        stored_counts = session.setdefault("dailyCounts", {})
        if not isinstance(stored_counts, dict):
            stored_counts = {}
            session["dailyCounts"] = stored_counts
        for current in _days(interval.start, interval.end):
            stored_counts[current.isoformat()] = normalized[current]
        completed = session.setdefault("completed", [])
        if not isinstance(completed, list):
            completed = []
            session["completed"] = completed
        record = _interval_record(interval.start, interval.end)
        completed[:] = [
            item
            for item in completed
            if not (
                isinstance(item, dict)
                and item.get("start") == record["start"]
                and item.get("end") == record["end"]
            )
        ]
        completed.append(record)
        session.pop("lastFailure", None)
        self.save()

    def record_failure(self, wxid: str, display_name: str, interval: DateInterval, reason: str) -> None:
        session = self._session(wxid, display_name)
        session["lastFailure"] = {
            "start": interval.start.isoformat(),
            "end": interval.end.isoformat(),
            "error": " ".join(str(reason).split())[:500],
        }
        self.save()

    def daily_counts(self, wxid: str, start: date, end: date) -> dict[date, int] | None:
        if self.uncovered(wxid, start, end):
            return None
        session = self.payload["sessions"].get(wxid, {})
        if not isinstance(session, dict):
            return None
        stored = session.get("dailyCounts", {})
        if not isinstance(stored, dict):
            return None
        counts: dict[date, int] = {}
        for current in _days(start, end):
            value = stored.get(current.isoformat())
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            counts[current] = value
        return counts

    def failure_reason(self, wxid: str) -> str | None:
        session = self.payload["sessions"].get(wxid, {})
        if not isinstance(session, dict):
            return None
        failure = session.get("lastFailure", {})
        if not isinstance(failure, dict):
            return None
        error = failure.get("error")
        return str(error) if error else None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(self.payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _session(self, wxid: str, display_name: str) -> dict[str, Any]:
        sessions = self.payload["sessions"]
        session = sessions.setdefault(wxid, {})
        if not isinstance(session, dict):
            session = {}
            sessions[wxid] = session
        if display_name:
            session["displayName"] = display_name
        return session

    def _completed_records(self, wxid: str) -> list[dict[str, Any]]:
        session = self.payload["sessions"].get(wxid, {})
        if not isinstance(session, dict):
            return []
        records = session.get("completed", [])
        return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []


def _normalize_daily_counts(interval: DateInterval, daily_counts: Mapping[date, int]) -> dict[date, int]:
    normalized: dict[date, int] = {}
    for current in _days(interval.start, interval.end):
        value = daily_counts.get(current, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"plan 的逐日消息数无效：{current}={value!r}")
        normalized[current] = value
    outside = [day for day in daily_counts if day < interval.start or day > interval.end]
    if outside:
        raise ValueError(f"plan 的逐日消息数越出请求区间：{outside[0]}")
    return normalized


def _interval_record(start: date, end: date) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat()}


def _uncovered_intervals(start: date, end: date, covered: set[date]) -> list[DateInterval]:
    intervals: list[DateInterval] = []
    cursor = start
    while cursor <= end:
        if cursor in covered:
            cursor += timedelta(days=1)
            continue
        interval_start = cursor
        while cursor <= end and cursor not in covered:
            cursor += timedelta(days=1)
        intervals.append(DateInterval(interval_start, cursor - timedelta(days=1)))
    return intervals


def _days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


__all__ = ["BulkExportPlan", "PLAN_VERSION"]
