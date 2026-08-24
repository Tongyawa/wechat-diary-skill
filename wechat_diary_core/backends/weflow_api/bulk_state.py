"""Resumable interval state for explicit bulk raw exports."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable


STATE_VERSION = 1


@dataclass(frozen=True)
class DateInterval:
    start: date
    end: date


class BulkExportState:
    """Track completed session intervals without touching daily failure state."""

    def __init__(self, path: Path, raw_root: Path, payload: dict[str, Any] | None = None) -> None:
        self.path = path
        self.raw_root = raw_root.resolve()
        self.payload = payload or {
            "version": STATE_VERSION,
            "outputRoot": str(self.raw_root),
            "sessions": {},
        }

    @classmethod
    def load(cls, path: Path, raw_root: Path) -> "BulkExportState":
        state_path = path.resolve()
        resolved_root = raw_root.resolve()
        if not state_path.exists():
            return cls(state_path, resolved_root)
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"批量导出状态文件不可读，拒绝覆盖：{state_path}（{exc}）") from exc
        if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
            raise ValueError(f"批量导出状态版本不受支持，拒绝覆盖：{state_path}")
        recorded_root = Path(str(payload.get("outputRoot") or "")).resolve()
        if recorded_root != resolved_root:
            raise ValueError(
                "批量导出状态属于其他输出根，拒绝复用："
                f"状态={recorded_root}；本次={resolved_root}"
            )
        if not isinstance(payload.get("sessions"), dict):
            raise ValueError(f"批量导出状态缺少 sessions 对象，拒绝覆盖：{state_path}")
        return cls(state_path, resolved_root, payload)

    def uncovered(self, wxid: str, start: date, end: date, *, force: bool) -> list[DateInterval]:
        if force:
            return [DateInterval(start, end)]
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

    def record_completed(
        self,
        wxid: str,
        display_name: str,
        interval: DateInterval,
        files: Iterable[Path],
    ) -> None:
        session = self._session(wxid, display_name)
        completed = session.setdefault("completed", [])
        if not isinstance(completed, list):
            completed = []
            session["completed"] = completed
        record = {
            "start": interval.start.isoformat(),
            "end": interval.end.isoformat(),
            "files": sorted(self._relative_file(path) for path in files),
        }
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

    def record_failure(
        self,
        wxid: str,
        display_name: str,
        interval: DateInterval,
        reason: str,
    ) -> None:
        session = self._session(wxid, display_name)
        session["lastFailure"] = {
            "start": interval.start.isoformat(),
            "end": interval.end.isoformat(),
            "error": " ".join(str(reason).split())[:500],
        }
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(self.payload, stream, ensure_ascii=False, indent=2)
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
        files = record.get("files")
        if not isinstance(files, list) or not all(isinstance(value, str) for value in files):
            return False
        for value in files:
            relative = Path(value)
            if relative.is_absolute() or ".." in relative.parts:
                return False
            candidate = (self.raw_root / relative).resolve()
            try:
                candidate.relative_to(self.raw_root)
            except ValueError:
                return False
            if not candidate.is_file():
                return False
        return True

    def _relative_file(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.raw_root)
        except ValueError as exc:
            raise ValueError(f"批量状态不能记录输出根之外的文件：{resolved}") from exc
        return relative.as_posix()


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


__all__ = ["BulkExportState", "DateInterval"]
