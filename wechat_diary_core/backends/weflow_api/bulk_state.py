"""Resumable interval state for explicit bulk raw exports."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping


STATE_VERSION = 2
JOURNAL_COMPACT_AFTER = 200


@dataclass(frozen=True)
class DateInterval:
    start: date
    end: date


class BulkExportState:
    """Track completed session intervals without touching daily failure state.

    Each completed block is durably appended to a sidecar journal. This keeps
    crash-safe block checkpoints from repeatedly serializing every prior output
    path as a bulk run grows. ``save()`` remains an atomic compacted snapshot.
    """

    def __init__(self, path: Path, raw_root: Path, payload: dict[str, Any] | None = None) -> None:
        self.path = path.resolve()
        self.raw_root = raw_root.resolve()
        self.journal_path = self.path.with_name(f"{self.path.name}.journal")
        self.payload = payload or {
            "version": STATE_VERSION,
            "outputRoot": str(self.raw_root),
            "sessions": {},
        }
        self._journal_events = 0

    @classmethod
    def load(cls, path: Path, raw_root: Path) -> "BulkExportState":
        state_path = path.resolve()
        resolved_root = raw_root.resolve()
        if not state_path.exists():
            if state_path.with_name(f"{state_path.name}.journal").exists():
                raise ValueError(f"批量导出状态快照缺失但 journal 存在，拒绝猜测恢复：{state_path}")
            return cls(state_path, resolved_root)
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"批量导出状态文件不可读，拒绝覆盖：{state_path}（{exc}）") from exc
        if not isinstance(payload, dict) or payload.get("version") not in {1, STATE_VERSION}:
            raise ValueError(f"批量导出状态版本不受支持，拒绝覆盖：{state_path}")
        recorded_root = Path(str(payload.get("outputRoot") or "")).resolve()
        if recorded_root != resolved_root:
            raise ValueError(
                "批量导出状态属于其他输出根，拒绝复用："
                f"状态={recorded_root}；本次={resolved_root}"
            )
        if not isinstance(payload.get("sessions"), dict):
            raise ValueError(f"批量导出状态缺少 sessions 对象，拒绝覆盖：{state_path}")
        _migrate_payload(payload)
        state = cls(state_path, resolved_root, payload)
        state._load_journal()
        return state

    def uncovered(self, wxid: str, start: date, end: date, *, force: bool) -> list[DateInterval]:
        if force:
            return [DateInterval(start, end)]
        covered = self.covered_days(wxid, start, end)
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

    def covered_days(self, wxid: str, start: date, end: date) -> set[date]:
        covered: set[date] = set()
        for record in self._valid_completed_records(wxid):
            record_start = _parse_day(record.get("start"))
            record_end = _parse_day(record.get("end"))
            if record_start is None or record_end is None or record_start > record_end:
                continue
            current = max(start, record_start)
            last = min(end, record_end)
            while current <= last:
                covered.add(current)
                current += timedelta(days=1)
        return covered

    def completed_daily_counts(self, wxid: str, start: date, end: date) -> dict[date, int] | None:
        """Return actual per-day counts only when every covered day has evidence."""

        counts: dict[date, int] = {}
        covered = self.covered_days(wxid, start, end)
        for record in self._valid_completed_records(wxid):
            record_start = _parse_day(record.get("start"))
            record_end = _parse_day(record.get("end"))
            daily_counts = record.get("dailyCounts")
            if record_start is None or record_end is None or not isinstance(daily_counts, dict):
                continue
            for current in _days(max(start, record_start), min(end, record_end)):
                # A valid completed interval proves every day was observed;
                # sparse absence inside it therefore means a real zero.
                value = daily_counts.get(current.isoformat(), 0)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    return None
                # Later records intentionally win after a --force re-export.
                counts[current] = value
        return counts if covered <= set(counts) else None

    def record_completed(
        self,
        wxid: str,
        display_name: str,
        interval: DateInterval,
        files: Iterable[Path],
        *,
        daily_counts: Mapping[date, int] | None = None,
    ) -> None:
        normalized_counts = _normalize_daily_counts(interval, daily_counts)
        # Canonical JSON is the authoritative manifest. Its media references
        # are rechecked by _files_exist(), so a deleted CAS object still makes
        # the block pending without storing every media path in the state file.
        canonical_files = [
            self._relative_file(path)
            for path in files
            if Path(path).suffix.lower() == ".json"
        ]
        record = {
            "start": interval.start.isoformat(),
            "end": interval.end.isoformat(),
            "files": sorted(set(canonical_files)),
            "dailyCounts": {
                day.isoformat(): count for day, count in normalized_counts.items() if count
            },
        }
        self._record_event(
            {"op": "completed", "wxid": wxid, "displayName": display_name, "record": record}
        )

    def record_failure(
        self, wxid: str, display_name: str, interval: DateInterval, reason: str
    ) -> None:
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

    def save(self) -> None:
        self.payload["version"] = STATE_VERSION
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
            if self.journal_path.exists():
                self.journal_path.unlink()
            self._journal_events = 0
        finally:
            if temporary.exists():
                temporary.unlink()

    def _record_event(self, event: dict[str, Any]) -> None:
        self._apply_event(event)
        if not self.path.exists():
            self.save()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            stream.write("\n")
        self._journal_events += 1
        if self._journal_events >= JOURNAL_COMPACT_AFTER:
            self.save()

    def _load_journal(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            text = self.journal_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"批量导出状态 journal 不可读，拒绝覆盖：{self.journal_path}（{exc}）") from exc
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1 and not text.endswith("\n"):
                    break
                raise ValueError(f"批量导出状态 journal 损坏，拒绝覆盖：{self.journal_path}") from exc
            if not isinstance(event, dict):
                raise ValueError(f"批量导出状态 journal 含无效事件，拒绝覆盖：{self.journal_path}")
            self._apply_event(event)
            self._journal_events += 1

    def _apply_event(self, event: Mapping[str, Any]) -> None:
        op = event.get("op")
        wxid = event.get("wxid")
        display_name = event.get("displayName")
        record = event.get("record")
        if not isinstance(wxid, str) or not isinstance(display_name, str) or not isinstance(record, dict):
            raise ValueError(f"批量导出状态 journal 事件字段无效：{self.journal_path}")
        session = self._session(wxid, display_name)
        if op == "completed":
            start, end = record.get("start"), record.get("end")
            if not isinstance(start, str) or not isinstance(end, str):
                raise ValueError(f"批量导出状态完成事件无效：{self.journal_path}")
            completed = session.setdefault("completed", [])
            if not isinstance(completed, list):
                completed = []
                session["completed"] = completed
            completed[:] = [
                item
                for item in completed
                if not (
                    isinstance(item, dict)
                    and item.get("start") == start
                    and item.get("end") == end
                )
            ]
            completed.append(_sparsify_record(dict(record)))
            session.pop("lastFailure", None)
            return
        if op == "failure":
            session["lastFailure"] = dict(record)
            return
        raise ValueError(f"批量导出状态 journal 事件类型无效：{self.journal_path}")

    def _session(self, wxid: str, display_name: str) -> dict[str, Any]:
        sessions = self.payload["sessions"]
        session = sessions.setdefault(wxid, {})
        if not isinstance(session, dict):
            session = {}
            sessions[wxid] = session
        session["displayName"] = display_name
        return session

    def _valid_completed_records(self, wxid: str) -> list[dict[str, Any]]:
        session = self.payload["sessions"].get(wxid, {})
        if not isinstance(session, dict):
            return []
        records = session.get("completed", [])
        if not isinstance(records, list):
            return []
        return [record for record in records if isinstance(record, dict) and self._files_exist(record)]

    def _files_exist(self, record: dict[str, Any]) -> bool:
        root = os.path.normcase(os.path.normpath(str(self.raw_root)))
        indexed_files = getattr(self, "_indexed_output_files", None)
        if indexed_files is None:
            # Do not follow junctions: a file reached through one is absent from
            # the normal store inventory and falls through to the resolved check
            # below. That retains the old boundary verdict for junction escapes.
            indexed_files = {
                os.path.normcase(os.path.normpath(os.path.join(directory, name)))
                for directory, _subdirectories, names in os.walk(self.raw_root, followlinks=False)
                for name in names
            }
            self._indexed_output_files = indexed_files

        def existing_output_file(path: Path) -> bool:
            candidate = os.path.normcase(os.path.normpath(str(path)))
            try:
                if os.path.commonpath((root, candidate)) != root:
                    return False
            except ValueError:
                return False
            # An indexed regular file needs no per-reference filesystem walk.
            # Symlinks and index misses retain the prior resolve-based safety
            # check; misses also cover outputs created after this index was built.
            if candidate in indexed_files and not os.path.islink(path):
                return True
            try:
                resolved = path.resolve()
                resolved.relative_to(self.raw_root)
            except (OSError, ValueError):
                return False
            if not resolved.is_file():
                return False
            indexed_files.add(candidate)
            return True

        files = record.get("files")
        if not isinstance(files, list) or not all(isinstance(value, str) for value in files):
            return False
        canonical_files: list[Path] = []
        for value in files:
            candidate = self.raw_root / Path(value)
            if not existing_output_file(candidate):
                return False
            if candidate.suffix.lower() == ".json":
                canonical_files.append(candidate)
        for json_path in canonical_files:
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return False
            messages = payload.get("messages") if isinstance(payload, dict) else None
            if not isinstance(messages, list):
                return False
            for message in messages:
                if not isinstance(message, Mapping):
                    return False
                reference = str(message.get("source") or "").strip()
                if not reference:
                    continue
                if not existing_output_file(json_path.parent / Path(reference)):
                    return False
        return True

    def _safe_output_path(self, value: str) -> Path | None:
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            return None
        candidate = (self.raw_root / relative).resolve()
        try:
            candidate.relative_to(self.raw_root)
        except ValueError:
            return None
        return candidate

    def _relative_file(self, path: Path) -> str:
        candidate = Path(path)
        try:
            relative = candidate.relative_to(self.raw_root)
        except ValueError:
            resolved = candidate.resolve()
            try:
                relative = resolved.relative_to(self.raw_root)
            except ValueError as exc:
                raise ValueError(f"批量状态不能记录输出根之外的文件：{resolved}") from exc
        return relative.as_posix()


def _normalize_daily_counts(
    interval: DateInterval, daily_counts: Mapping[date, int] | None
) -> dict[date, int]:
    values = daily_counts or {}
    normalized: dict[date, int] = {}
    current = interval.start
    while current <= interval.end:
        value = values.get(current, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"批量导出实际逐日消息数无效：{current}={value!r}")
        normalized[current] = value
        current += timedelta(days=1)
    outside = [day for day in values if day < interval.start or day > interval.end]
    if outside:
        raise ValueError(f"批量导出实际逐日消息数越出区间：{outside[0]}")
    return normalized


def _migrate_payload(payload: dict[str, Any]) -> None:
    payload["version"] = STATE_VERSION
    _sparsify_payload(payload)


def _sparsify_payload(payload: dict[str, Any]) -> None:
    sessions = payload.get("sessions", {})
    if not isinstance(sessions, dict):
        return
    for session in sessions.values():
        records = session.get("completed", []) if isinstance(session, dict) else []
        if isinstance(records, list):
            for index, record in enumerate(records):
                if isinstance(record, dict):
                    records[index] = _sparsify_record(record)


def _sparsify_record(record: dict[str, Any]) -> dict[str, Any]:
    daily_counts = record.get("dailyCounts")
    if isinstance(daily_counts, dict):
        record["dailyCounts"] = {
            str(day): value
            for day, value in daily_counts.items()
            if not (isinstance(value, int) and not isinstance(value, bool) and value == 0)
        }
    return record


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


__all__ = ["BulkExportState", "DateInterval"]
