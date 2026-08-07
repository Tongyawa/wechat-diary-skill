"""Workspace-local state for repeated WeFlow session export failures."""

from __future__ import annotations

import json
import os
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Iterable


STATE_VERSION = 1
REVIEW_THRESHOLD = 3


class SessionFailureState:
    """Track distinct failed export dates and explicit ignore authorization."""

    def __init__(
        self,
        path: Path,
        *,
        failures: dict[str, dict[str, Any]] | None = None,
        ignored: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.path = path
        self.failures = failures or {}
        self.ignored = ignored or {}
        self._dirty = False

    @classmethod
    def load(cls, path: Path) -> "SessionFailureState":
        if not path.is_file():
            return cls(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("导出状态文件顶层必须是 object")
        failures = payload.get("failures") or {}
        ignored = payload.get("ignored") or {}
        if not isinstance(failures, dict) or not isinstance(ignored, dict):
            raise ValueError("导出状态文件 failures/ignored 必须是 object")
        return cls(
            path,
            failures={str(key): _normalize_record(str(key), value) for key, value in failures.items()},
            ignored={str(key): _normalize_record(str(key), value) for key, value in ignored.items()},
        )

    def is_ignored(self, wxid: str) -> bool:
        return wxid in self.ignored

    def record_failure(
        self,
        wxid: str,
        display_name: str,
        export_date: date,
        error: str,
    ) -> dict[str, Any]:
        day = export_date.isoformat()
        previous = self.failures.get(wxid, {})
        failure_dates = sorted({*previous.get("failureDates", []), day})
        record = _build_record(
            wxid,
            display_name,
            failure_dates,
            _error_summary(error),
        )
        if record != previous:
            self.failures[wxid] = record
            self._dirty = True
        if wxid in self.ignored:
            ignored_at = str(self.ignored[wxid].get("ignoredAtDate") or day)
            ignored_record = {**record, "ignoredAtDate": ignored_at}
            if ignored_record != self.ignored[wxid]:
                self.ignored[wxid] = ignored_record
                self._dirty = True
        return record

    def record_success(self, wxid: str) -> bool:
        was_ignored = wxid in self.ignored
        if self.failures.pop(wxid, None) is not None:
            self._dirty = True
        if self.ignored.pop(wxid, None) is not None:
            self._dirty = True
        return was_ignored

    def pending_review(self, threshold: int = REVIEW_THRESHOLD) -> list[dict[str, Any]]:
        return sorted(
            (
                record
                for wxid, record in self.failures.items()
                if wxid not in self.ignored and int(record.get("consecutiveFailures") or 0) >= threshold
            ),
            key=lambda record: (-int(record["consecutiveFailures"]), str(record["displayName"])),
        )

    def ignore(self, wxids: Iterable[str], *, authorized_date: date) -> int:
        changed = 0
        for wxid in wxids:
            record = self.failures.get(wxid)
            if record is None:
                continue
            ignored_record = {**record, "ignoredAtDate": authorized_date.isoformat()}
            if self.ignored.get(wxid) != ignored_record:
                self.ignored[wxid] = ignored_record
                self._dirty = True
                changed += 1
        return changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "failures": self.failures,
            "ignored": self.ignored,
        }

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
        finally:
            temp_path.unlink(missing_ok=True)
        self._dirty = False


def _normalize_record(wxid: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"导出状态记录必须是 object：{wxid}")
    dates = sorted({str(item) for item in value.get("failureDates") or [] if str(item)})
    record = _build_record(
        wxid,
        str(value.get("displayName") or wxid),
        dates,
        str(value.get("lastError") or "未知错误"),
    )
    ignored_at = str(value.get("ignoredAtDate") or "")
    if ignored_at:
        record["ignoredAtDate"] = ignored_at
    return record


def _build_record(
    wxid: str,
    display_name: str,
    failure_dates: list[str],
    error: str,
) -> dict[str, Any]:
    return {
        "wxid": wxid,
        "displayName": display_name or wxid,
        "consecutiveFailures": len(failure_dates),
        "failureDates": failure_dates,
        "firstFailureDate": failure_dates[0] if failure_dates else "",
        "lastFailureDate": failure_dates[-1] if failure_dates else "",
        "lastError": _error_summary(error),
    }


def _error_summary(value: str, limit: int = 500) -> str:
    text = " ".join(str(value or "未知错误").split())
    return text[:limit]


__all__ = ["REVIEW_THRESHOLD", "STATE_VERSION", "SessionFailureState"]
