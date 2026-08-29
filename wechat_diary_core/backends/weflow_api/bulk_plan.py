"""Persistent, inspectable message-count plans for chunked bulk exports."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

from .bulk_state import DateInterval


PLAN_VERSION = 2
JOURNAL_COMPACT_AFTER = 200


class BulkExportPlan:
    """Store nonzero daily counts; completed intervals prove zero-day coverage."""

    def __init__(self, path: Path, raw_root: Path, payload: dict[str, Any] | None = None) -> None:
        self.path = path.resolve()
        self.raw_root = raw_root.resolve()
        self.journal_path = self.path.with_name(f"{self.path.name}.journal")
        self.payload = payload or {
            "version": PLAN_VERSION,
            "outputRoot": str(self.raw_root),
            "sessions": {},
            "noLocalRecords": {},
        }
        self._journal_events = 0

    @classmethod
    def load(cls, path: Path, raw_root: Path) -> "BulkExportPlan":
        plan_path = path.resolve()
        resolved_root = raw_root.resolve()
        if not plan_path.exists():
            if plan_path.with_name(f"{plan_path.name}.journal").exists():
                raise ValueError(f"批量导出 plan 快照缺失但 journal 存在，拒绝猜测恢复：{plan_path}")
            return cls(plan_path, resolved_root)
        try:
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"批量导出 plan 文件不可读，拒绝覆盖：{plan_path}（{exc}）") from exc
        if not isinstance(payload, dict) or payload.get("version") not in {1, PLAN_VERSION}:
            raise ValueError(f"批量导出 plan 版本不受支持，拒绝覆盖：{plan_path}")
        recorded_root = Path(str(payload.get("outputRoot") or "")).resolve()
        if recorded_root != resolved_root:
            raise ValueError(
                "批量导出 plan 属于其他输出根，拒绝复用："
                f"plan={recorded_root}；本次={resolved_root}"
            )
        if not isinstance(payload.get("sessions"), dict):
            raise ValueError(f"批量导出 plan 缺少 sessions 对象，拒绝覆盖：{plan_path}")
        _migrate_payload(payload)
        plan = cls(plan_path, resolved_root, payload)
        plan._load_journal()
        return plan

    def resolve_range(
        self,
        requested_start: date | None,
        requested_end: date | None,
        *,
        allow_expand: bool,
    ) -> tuple[date, date, DateInterval | None]:
        if (requested_start is None) != (requested_end is None):
            raise ValueError("--start 与 --end 必须同时提供或同时省略")
        recorded = self.recorded_range()
        if recorded is None:
            if requested_start is None or requested_end is None:
                raise ValueError("尚无批量导出 plan；请首次运行时显式提供 --start 与 --end")
            if requested_start > requested_end:
                raise ValueError("批量导出的起始日期不能晚于结束日期")
            self.payload["range"] = _interval_record(requested_start, requested_end)
            return requested_start, requested_end, None
        if requested_start is None or requested_end is None:
            return recorded.start, recorded.end, None
        if requested_start > requested_end:
            raise ValueError("批量导出的起始日期不能晚于结束日期")
        if (requested_start, requested_end) == (recorded.start, recorded.end):
            return requested_start, requested_end, None
        mismatch = (
            f"plan 区间是 {recorded.start}~{recorded.end}，"
            f"本次请求 {requested_start}~{requested_end}"
        )
        if not allow_expand:
            raise ValueError(f"{mismatch}；默认拒绝改动，请确认后显式加 --expand-range")
        if requested_start > recorded.start or requested_end < recorded.end:
            raise ValueError(f"{mismatch}；区间只能扩、不能缩")
        self.payload["range"] = _interval_record(requested_start, requested_end)
        return requested_start, requested_end, recorded

    def recorded_range(self) -> DateInterval | None:
        value = self.payload.get("range")
        if not isinstance(value, dict):
            return None
        start = _parse_day(value.get("start"))
        end = _parse_day(value.get("end"))
        if start is None or end is None or start > end:
            raise ValueError(f"批量导出 plan 的 range 无效：{self.path}")
        return DateInterval(start, end)

    def uncovered(self, wxid: str, start: date, end: date) -> list[DateInterval]:
        covered: set[date] = set()
        for record in self._completed_records(wxid):
            record_start = _parse_day(record.get("start"))
            record_end = _parse_day(record.get("end"))
            if record_start is None or record_end is None or record_start > record_end:
                continue
            current, last = max(start, record_start), min(end, record_end)
            while current <= last:
                covered.add(current)
                current += timedelta(days=1)
        return _uncovered_intervals(start, end, covered)

    def invalidate(self, wxid: str, start: date, end: date) -> None:
        self._record_event(
            {"op": "invalidate", "wxid": wxid, "start": start.isoformat(), "end": end.isoformat()}
        )

    def record_completed(
        self,
        wxid: str,
        display_name: str,
        interval: DateInterval,
        daily_counts: Mapping[date, int],
    ) -> None:
        normalized = _normalize_daily_counts(interval, daily_counts)
        self._record_event(
            {
                "op": "completed",
                "wxid": wxid,
                "displayName": display_name,
                "record": _interval_record(interval.start, interval.end),
                "dailyCounts": {day.isoformat(): count for day, count in normalized.items() if count},
            }
        )

    def record_failure(self, wxid: str, display_name: str, interval: DateInterval, reason: str) -> None:
        self._record_event(
            {
                "op": "failure",
                "wxid": wxid,
                "displayName": display_name,
                "record": {
                    "start": interval.start.isoformat(),
                    "end": interval.end.isoformat(),
                    "error": " ".join(str(reason).split())[:500],
                },
            }
        )

    def record_no_local_records(
        self,
        wxid: str,
        display_name: str,
        detected_date: date,
        reason: str,
        *,
        last_timestamp: int | None,
        first_detected_date: str | None = None,
    ) -> None:
        previous = self._no_local_records().get(wxid, {})
        self._record_event(
            {
                "op": "no_local_records",
                "wxid": wxid,
                "record": {
                    "displayName": display_name or wxid,
                    "firstDetectedDate": str(
                        first_detected_date
                        or (previous.get("firstDetectedDate") if isinstance(previous, dict) else "")
                        or detected_date.isoformat()
                    ),
                    "lastDetectedDate": detected_date.isoformat(),
                    "lastTimestamp": last_timestamp,
                    "lastError": " ".join(str(reason).split())[:500],
                },
            }
        )

    def clear_no_local_records(self, wxid: str) -> None:
        if self.is_no_local_records(wxid):
            self._record_event({"op": "clear_no_local_records", "wxid": wxid})

    def is_no_local_records(self, wxid: str) -> bool:
        return wxid in self._no_local_records()

    def no_local_records_count(self, wxids: set[str] | None = None) -> int:
        records = self._no_local_records()
        return len(records) if wxids is None else sum(wxid in records for wxid in wxids)

    def has_completed(self, wxid: str) -> bool:
        return bool(self._completed_records(wxid))

    def daily_counts(self, wxid: str, start: date, end: date) -> dict[date, int] | None:
        # Coverage is the proof that a missing sparse key is a real zero.
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
            value = stored.get(current.isoformat(), 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            counts[current] = value
        return counts

    def failure_reason(self, wxid: str) -> str | None:
        session = self.payload["sessions"].get(wxid, {})
        failure = session.get("lastFailure", {}) if isinstance(session, dict) else {}
        error = failure.get("error") if isinstance(failure, dict) else None
        return str(error) if error else None

    def save(self) -> None:
        self.payload["version"] = PLAN_VERSION
        _sparsify_payload(self.payload)
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
            self.journal_path.unlink(missing_ok=True)
            self._journal_events = 0
        finally:
            temporary.unlink(missing_ok=True)

    def _record_event(self, event: dict[str, Any]) -> None:
        self._apply_event(event)
        if not self.path.exists():
            self.save()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        self._journal_events += 1
        if self._journal_events >= JOURNAL_COMPACT_AFTER:
            self.save()

    def _load_journal(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            text = self.journal_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"批量导出 plan journal 不可读，拒绝覆盖：{self.journal_path}（{exc}）") from exc
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1 and not text.endswith("\n"):
                    break
                raise ValueError(f"批量导出 plan journal 损坏，拒绝覆盖：{self.journal_path}") from exc
            if not isinstance(event, dict):
                raise ValueError(f"批量导出 plan journal 含无效事件：{self.journal_path}")
            self._apply_event(event)
            self._journal_events += 1

    def _apply_event(self, event: Mapping[str, Any]) -> None:
        op, wxid = event.get("op"), event.get("wxid")
        if not isinstance(wxid, str) or not wxid:
            raise ValueError(f"批量导出 plan journal 事件缺少 wxid：{self.journal_path}")
        if op == "clear_no_local_records":
            self._no_local_records().pop(wxid, None)
            return
        if op == "no_local_records":
            record = event.get("record")
            if not isinstance(record, dict):
                raise ValueError(f"批量导出 plan 本机无记录事件无效：{self.journal_path}")
            self._no_local_records()[wxid] = dict(record)
            self.payload["sessions"].pop(wxid, None)
            return
        display_name = event.get("displayName")
        if op != "invalidate" and not isinstance(display_name, str):
            raise ValueError(f"批量导出 plan journal 事件字段无效：{self.journal_path}")
        session = self._session(wxid, str(display_name or ""))
        if op == "invalidate":
            start, end = _parse_day(event.get("start")), _parse_day(event.get("end"))
            if start is None or end is None or start > end:
                raise ValueError(f"批量导出 plan 失效事件无效：{self.journal_path}")
            stored = session.get("dailyCounts", {})
            if isinstance(stored, dict):
                for current in _days(start, end):
                    stored.pop(current.isoformat(), None)
            session["completed"] = _subtract_records(self._completed_records(wxid), start, end)
            session.pop("lastFailure", None)
            return
        if op == "completed":
            record, sparse = event.get("record"), event.get("dailyCounts")
            if not isinstance(record, dict) or not isinstance(sparse, dict):
                raise ValueError(f"批量导出 plan 完成事件无效：{self.journal_path}")
            start, end = _parse_day(record.get("start")), _parse_day(record.get("end"))
            if start is None or end is None or start > end:
                raise ValueError(f"批量导出 plan 完成区间无效：{self.journal_path}")
            stored = session.setdefault("dailyCounts", {})
            if not isinstance(stored, dict):
                stored = session["dailyCounts"] = {}
            for current in _days(start, end):
                stored.pop(current.isoformat(), None)
            for key, value in sparse.items():
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError(f"批量导出 plan 稀疏计数无效：{key}={value!r}")
                stored[str(key)] = value
            completed = session.setdefault("completed", [])
            if not isinstance(completed, list):
                completed = session["completed"] = []
            completed[:] = [
                item for item in completed
                if not (isinstance(item, dict) and item.get("start") == record.get("start")
                        and item.get("end") == record.get("end"))
            ]
            completed.append(dict(record))
            session.pop("lastFailure", None)
            self._no_local_records().pop(wxid, None)
            return
        if op == "failure":
            record = event.get("record")
            if not isinstance(record, dict):
                raise ValueError(f"批量导出 plan 失败事件无效：{self.journal_path}")
            session["lastFailure"] = dict(record)
            self._no_local_records().pop(wxid, None)
            return
        raise ValueError(f"批量导出 plan journal 事件类型无效：{self.journal_path}")

    def _session(self, wxid: str, display_name: str) -> dict[str, Any]:
        sessions = self.payload["sessions"]
        session = sessions.setdefault(wxid, {})
        if not isinstance(session, dict):
            session = sessions[wxid] = {}
        if display_name:
            session["displayName"] = display_name
        return session

    def _completed_records(self, wxid: str) -> list[dict[str, Any]]:
        session = self.payload["sessions"].get(wxid, {})
        records = session.get("completed", []) if isinstance(session, dict) else []
        return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []

    def _no_local_records(self) -> dict[str, dict[str, Any]]:
        records = self.payload.setdefault("noLocalRecords", {})
        if not isinstance(records, dict):
            raise ValueError(f"批量导出 plan 的 noLocalRecords 必须是 object：{self.path}")
        return records


def _migrate_payload(payload: dict[str, Any]) -> None:
    payload["version"] = PLAN_VERSION
    payload.setdefault("noLocalRecords", {})
    if not isinstance(payload["noLocalRecords"], dict):
        raise ValueError("批量导出 plan 的 noLocalRecords 必须是 object")
    if "range" not in payload:
        starts: list[date] = []
        ends: list[date] = []
        for session in payload["sessions"].values():
            records = session.get("completed", []) if isinstance(session, dict) else []
            if not isinstance(records, list):
                continue
            for record in records:
                start = _parse_day(record.get("start")) if isinstance(record, dict) else None
                end = _parse_day(record.get("end")) if isinstance(record, dict) else None
                if start is not None and end is not None and start <= end:
                    starts.append(start)
                    ends.append(end)
        if starts:
            payload["range"] = _interval_record(min(starts), max(ends))
    _sparsify_payload(payload)


def _sparsify_payload(payload: dict[str, Any]) -> None:
    sessions = payload.get("sessions", {})
    if not isinstance(sessions, dict):
        return
    for session in sessions.values():
        stored = session.get("dailyCounts") if isinstance(session, dict) else None
        if isinstance(stored, dict):
            session["dailyCounts"] = {
                str(day): value
                for day, value in stored.items()
                if not (isinstance(value, int) and not isinstance(value, bool) and value == 0)
            }


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


def _subtract_records(records: list[dict[str, Any]], start: date, end: date) -> list[dict[str, str]]:
    replacement: list[dict[str, str]] = []
    for record in records:
        record_start, record_end = _parse_day(record.get("start")), _parse_day(record.get("end"))
        if record_start is None or record_end is None:
            continue
        if record_end < start or end < record_start:
            replacement.append(_interval_record(record_start, record_end))
        else:
            if record_start < start:
                replacement.append(_interval_record(record_start, start - timedelta(days=1)))
            if end < record_end:
                replacement.append(_interval_record(end + timedelta(days=1), record_end))
    return replacement


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


__all__ = ["BulkExportPlan", "JOURNAL_COMPACT_AFTER", "PLAN_VERSION"]
