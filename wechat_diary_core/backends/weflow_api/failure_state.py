"""Workspace-local state for repeated WeFlow session export failures."""

from __future__ import annotations

import json
import hashlib
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable


STATE_VERSION = 3
REVIEW_THRESHOLD = 3


@dataclass(frozen=True)
class FailureUpdate:
    record: dict[str, Any]
    was_ignored: bool
    fingerprint_changed: bool


class SessionFailureState:
    """Track distinct failed export dates and explicit ignore authorization."""

    def __init__(
        self,
        path: Path,
        *,
        failures: dict[str, dict[str, Any]] | None = None,
        ignored: dict[str, dict[str, Any]] | None = None,
        no_local_records: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.path = path
        self.failures = failures or {}
        self.ignored = ignored or {}
        self.no_local_records = no_local_records or {}
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
        no_local_records = payload.get("noLocalRecords") or {}
        if not all(isinstance(value, dict) for value in (failures, ignored, no_local_records)):
            raise ValueError("导出状态文件 failures/ignored/noLocalRecords 必须是 object")
        return cls(
            path,
            failures={str(key): _normalize_record(str(key), value) for key, value in failures.items()},
            ignored={str(key): _normalize_record(str(key), value) for key, value in ignored.items()},
            no_local_records={
                str(key): _normalize_no_local_record(str(key), value)
                for key, value in no_local_records.items()
            },
        )

    def is_ignored(self, wxid: str) -> bool:
        return wxid in self.ignored

    def is_no_local_records(self, wxid: str) -> bool:
        return wxid in self.no_local_records

    def record_failure(
        self,
        wxid: str,
        display_name: str,
        export_date: date,
        error: str,
    ) -> FailureUpdate:
        day = export_date.isoformat()
        previous = self.failures.get(wxid, {})
        previous_ignored = self.ignored.get(wxid)
        if self.no_local_records.pop(wxid, None) is not None:
            self._dirty = True
        fingerprint = error_fingerprint(error)
        previous_fingerprint = str((previous_ignored or {}).get("errorFingerprint") or "")
        fingerprint_changed = bool(previous_ignored and previous_fingerprint and previous_fingerprint != fingerprint)
        failure_dates = (
            [day]
            if fingerprint_changed
            else sorted({*previous.get("failureDates", []), day})
        )
        record = _build_record(
            wxid,
            display_name,
            failure_dates,
            _error_summary(error),
            fingerprint,
        )
        if record != previous:
            self.failures[wxid] = record
            self._dirty = True
        if fingerprint_changed:
            self.ignored.pop(wxid, None)
            self._dirty = True
        elif previous_ignored is not None:
            ignored_at = str(previous_ignored.get("ignoredAtDate") or day)
            ignored_record = {**record, "ignoredAtDate": ignored_at}
            if ignored_record != previous_ignored:
                self.ignored[wxid] = ignored_record
                self._dirty = True
        return FailureUpdate(
            record=record,
            was_ignored=previous_ignored is not None,
            fingerprint_changed=fingerprint_changed,
        )

    def record_success(self, wxid: str) -> bool:
        was_ignored = wxid in self.ignored
        if self.failures.pop(wxid, None) is not None:
            self._dirty = True
        if self.ignored.pop(wxid, None) is not None:
            self._dirty = True
        if self.no_local_records.pop(wxid, None) is not None:
            self._dirty = True
        return was_ignored

    def record_no_local_records(
        self,
        wxid: str,
        display_name: str,
        detected_date: date,
        error: str,
        *,
        last_timestamp: int | None,
    ) -> bool:
        """Move a conservatively identified empty session out of failures."""

        day = detected_date.isoformat()
        previous = self.no_local_records.get(wxid)
        record = {
            "wxid": wxid,
            "displayName": display_name or wxid,
            "firstDetectedDate": str((previous or {}).get("firstDetectedDate") or day),
            "lastDetectedDate": day,
            "lastTimestamp": last_timestamp,
            "lastError": _error_summary(error),
        }
        if record != previous:
            self.no_local_records[wxid] = record
            self._dirty = True
        if self.failures.pop(wxid, None) is not None:
            self._dirty = True
        if self.ignored.pop(wxid, None) is not None:
            self._dirty = True
        return previous is None

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
            "noLocalRecords": self.no_local_records,
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
        str(value.get("errorFingerprint") or ""),
    )
    ignored_at = str(value.get("ignoredAtDate") or "")
    if ignored_at:
        record["ignoredAtDate"] = ignored_at
    return record


def _normalize_no_local_record(wxid: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"导出状态记录必须是 object：{wxid}")
    raw_timestamp = value.get("lastTimestamp")
    if raw_timestamp is None:
        last_timestamp = None
    elif isinstance(raw_timestamp, bool):
        raise ValueError(f"本机无记录会话 lastTimestamp 必须是整数或 null：{wxid}")
    else:
        try:
            last_timestamp = int(raw_timestamp)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"本机无记录会话 lastTimestamp 必须是整数或 null：{wxid}") from exc
    first_detected = str(value.get("firstDetectedDate") or "")
    last_detected = str(value.get("lastDetectedDate") or first_detected)
    return {
        "wxid": wxid,
        "displayName": str(value.get("displayName") or wxid),
        "firstDetectedDate": first_detected,
        "lastDetectedDate": last_detected,
        "lastTimestamp": last_timestamp,
        "lastError": _error_summary(str(value.get("lastError") or "未知错误")),
    }


def _build_record(
    wxid: str,
    display_name: str,
    failure_dates: list[str],
    error: str,
    fingerprint: str,
) -> dict[str, Any]:
    record = {
        "wxid": wxid,
        "displayName": display_name or wxid,
        "consecutiveFailures": len(failure_dates),
        "failureDates": failure_dates,
        "firstFailureDate": failure_dates[0] if failure_dates else "",
        "lastFailureDate": failure_dates[-1] if failure_dates else "",
        "lastError": _error_summary(error),
    }
    if fingerprint:
        record["errorFingerprint"] = fingerprint
    return record


def _error_summary(value: str, limit: int = 500) -> str:
    text = " ".join(str(value or "未知错误").split())
    return text[:limit]


_WINDOWS_PATH_RE = re.compile(r"(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\)[^\s]+")
_POSIX_PATH_RE = re.compile(r"(?<![:\w])/(?:[^/\s]+/)*[^\s]+")
_IDENTITY_RE = re.compile(r"(?i)(?<![a-z0-9_])(?:wxid|gh)_[a-z0-9_-]+")
_UUID_RE = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_LONG_HEX_RE = re.compile(r"(?i)\b[0-9a-f]{8,}\b")
_NUMBER_RE = re.compile(r"(?<![\w])[-+]?\d+(?:\.\d+)?(?![\w])")


def normalize_error_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "未知错误")).lower()
    text = _WINDOWS_PATH_RE.sub("<path>", text)
    text = _POSIX_PATH_RE.sub("<path>", text)
    text = _IDENTITY_RE.sub("<id>", text)
    text = _UUID_RE.sub("<uuid>", text)
    text = _LONG_HEX_RE.sub("<hex>", text)
    text = _NUMBER_RE.sub("<num>", text)
    return " ".join(text.split())


def error_fingerprint(value: str) -> str:
    normalized = normalize_error_text(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


__all__ = [
    "FailureUpdate",
    "REVIEW_THRESHOLD",
    "STATE_VERSION",
    "SessionFailureState",
    "error_fingerprint",
    "normalize_error_text",
]
