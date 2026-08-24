"""Detect archive session folders that represent the same canonical wxid.

The archive intentionally still uses human-readable folder names. This module
does not rename or merge anything: it only records identity conflicts so a
person can decide how to reconcile historical folders later.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


STATE_VERSION = 1


@dataclass(frozen=True)
class SessionRenameConflict:
    wxid: str
    directories: tuple[str, ...]

    @property
    def fingerprint(self) -> str:
        payload = "\0".join((self.wxid, *self.directories))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "wxid": self.wxid,
            "directories": list(self.directories),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class SessionRenameAlarmReport:
    new_conflicts: tuple[SessionRenameConflict, ...]
    stable_conflict_count: int
    scan_error_count: int
    state_error: str | None


def inspect_archive_session_names(archive_raw_root: str | Path) -> list[SessionRenameConflict]:
    """Return wxids that occur in two or more archive session directories.

    Only JSON files directly inside a first-level archive session directory are
    candidates. This deliberately excludes root-level moments data and JSON
    that happens to live in a session's media subtree.
    """

    root = Path(archive_raw_root)
    if not root.is_dir():
        return []

    directories_by_wxid: dict[str, set[str]] = {}
    for session_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        for json_path in sorted(session_dir.glob("*.json")):
            wxid = _read_session_wxid(json_path)
            if wxid:
                directories_by_wxid.setdefault(wxid, set()).add(session_dir.name)

    conflicts = [
        SessionRenameConflict(
            wxid=wxid,
            directories=tuple(sorted(directories)),
        )
        for wxid, directories in directories_by_wxid.items()
        if len(directories) >= 2
    ]
    return sorted(conflicts, key=lambda conflict: (conflict.directories, conflict.wxid))


def update_session_rename_alarm(
    archive_raw_root: str | Path,
    state_path: str | Path,
) -> SessionRenameAlarmReport:
    """Persist newly reported conflicts and return only those needing attention.

    The state is workspace-local because it contains private wxids and display
    names. A state problem is reported to the caller but never raises: this
    check is advisory and must not alter the daily export result.
    """

    scan_errors = 0
    try:
        conflicts = inspect_archive_session_names(archive_raw_root)
    except OSError:
        # Directory enumeration failures are intentionally non-fatal. Treat
        # them as a visible failed check rather than claiming a clean scan.
        conflicts = []
        scan_errors = 1

    path = Path(state_path)
    try:
        previous = _load_state(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        previous = {}
        state_error = f"无法读取告警状态：{exc}"
    else:
        state_error = None

    current = {conflict.wxid: conflict for conflict in conflicts}
    new_conflicts = tuple(
        conflict
        for conflict in conflicts
        if str((previous.get(conflict.wxid) or {}).get("fingerprint") or "") != conflict.fingerprint
    )
    stable_count = len(conflicts) - len(new_conflicts)
    updated = {wxid: conflict.to_dict() for wxid, conflict in current.items()}

    if updated != previous:
        try:
            _save_state(path, updated)
        except OSError as exc:
            state_error = state_error or f"无法保存告警状态：{exc}"

    return SessionRenameAlarmReport(
        new_conflicts=new_conflicts,
        stable_conflict_count=stable_count,
        scan_error_count=scan_errors,
        state_error=state_error,
    )


def _read_session_wxid(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    session = payload.get("session")
    if not isinstance(session, dict):
        return ""
    return str(session.get("wxid") or "").strip()


def _load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        raise ValueError("告警状态文件格式或版本不兼容")
    records = payload.get("reported")
    if not isinstance(records, dict):
        raise ValueError("告警状态文件 reported 必须是 object")
    return {str(wxid): value for wxid, value in records.items() if isinstance(value, dict)}


def _save_state(path: Path, records: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps({"version": STATE_VERSION, "reported": records}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "STATE_VERSION",
    "SessionRenameAlarmReport",
    "SessionRenameConflict",
    "inspect_archive_session_names",
    "update_session_rename_alarm",
]
