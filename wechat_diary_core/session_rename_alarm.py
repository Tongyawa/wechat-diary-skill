"""Reconcile archive session folders that represent the same canonical wxid.

Archives retain human-readable directory names. When a current raw export
supplies an unambiguous display name for a wxid, this module safely merges its
historical directories into that current name while retaining advisory state
for splits that cannot yet be resolved.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archiving import strip_date_suffix
from .backends.weflow.naming import sanitize_session_name


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


@dataclass(frozen=True)
class SessionRenameMerge:
    wxid: str
    target_directory: str
    source_directories: tuple[str, ...]
    moved_files: int
    duplicate_files: int
    rejected_files: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SessionRenameStay:
    wxid: str
    directories: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class SessionRenameReconciliation:
    merges: tuple[SessionRenameMerge, ...]
    stayed: tuple[SessionRenameStay, ...]

    @property
    def has_reportable_items(self) -> bool:
        return bool(self.merges or self.stayed)


def inspect_archive_session_names(archive_raw_root: str | Path) -> list[SessionRenameConflict]:
    """Return wxids that occur in two or more archive session directories.

    Only JSON files directly inside a first-level archive session directory are
    candidates. This deliberately excludes root-level moments data and JSON
    that happens to live in a session's media subtree.
    """

    root = Path(archive_raw_root)
    if not root.is_dir():
        return []

    directories_by_wxid = _archive_session_directories(root)

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


def reconcile_archive_session_names(
    archive_raw_root: str | Path,
    current_raw_root: str | Path,
    *,
    find_guard_failures: Callable[[list[tuple[Path, Path]]], list[Any]],
    files_equal: Callable[[Path, Path], bool],
) -> SessionRenameReconciliation:
    """Merge known archive splits into this run's actual display-name directory.

    The caller supplies the ingest guard from ``archive_exports.py`` so this
    action uses the exact same collision policy as historical ingestion. A
    session without one unambiguous current display name is deliberately left
    untouched until a future export supplies one.
    """

    archive_root = Path(archive_raw_root)
    directories_by_wxid = _archive_session_directories(archive_root)
    targets, target_issues = _current_session_targets(Path(current_raw_root))
    merges: list[SessionRenameMerge] = []
    stayed: list[SessionRenameStay] = []

    for wxid, directories in sorted(directories_by_wxid.items()):
        target_directory = targets.get(wxid)
        if target_directory is None:
            if len(directories) >= 2:
                stayed.append(
                    SessionRenameStay(
                        wxid=wxid,
                        directories=tuple(sorted(directories)),
                        reason=target_issues.get(wxid, "本轮未导出该会话，尚无新 displayName"),
                    )
                )
            continue
        source_directories = tuple(sorted(name for name in directories if name != target_directory))
        if not source_directories:
            continue
        merges.append(
            _merge_session_directories(
                archive_root,
                wxid=wxid,
                target_directory=target_directory,
                source_directories=source_directories,
                find_guard_failures=find_guard_failures,
                files_equal=files_equal,
            )
        )

    return SessionRenameReconciliation(merges=tuple(merges), stayed=tuple(stayed))


def write_session_rename_report(
    report: SessionRenameReconciliation,
    path: str | Path,
) -> Path:
    """Write private merge details for the user to inspect and manually close."""

    report_path = Path(path)
    lines = ["# 会话归档自动合并报告", ""]
    if report.merges:
        lines.extend(["## 本轮合并", ""])
        for merge in report.merges:
            lines.append(f"- wxid: {merge.wxid}")
            lines.append(f"  新目录: {merge.target_directory}")
            lines.append(f"  旧目录: {', '.join(merge.source_directories)}")
            lines.append(f"  已移动文件: {merge.moved_files}；重复保留目标副本: {merge.duplicate_files}")
            if merge.rejected_files:
                lines.append(f"  护栏拒绝: {len(merge.rejected_files)} 个文件（原目录已保留）")
                for relative_path, reason in merge.rejected_files:
                    lines.append(f"    - {relative_path}: {reason}")
    if report.stayed:
        lines.extend(["", "## 停留", ""])
        for stayed in report.stayed:
            lines.append(f"- wxid: {stayed.wxid}")
            lines.append(f"  现有目录: {', '.join(stayed.directories)}")
            lines.append(f"  原因: {stayed.reason}")
    lines.append("")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_name(f".{report_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text("\n".join(lines), encoding="utf-8")
        os.replace(temporary, report_path)
    finally:
        temporary.unlink(missing_ok=True)
    return report_path


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


def _archive_session_directories(root: Path) -> dict[str, set[str]]:
    if not root.is_dir():
        return {}
    directories_by_wxid: dict[str, set[str]] = {}
    for session_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        for json_path in sorted(session_dir.glob("*.json")):
            wxid = _read_session_wxid(json_path)
            if wxid:
                directories_by_wxid.setdefault(wxid, set()).add(session_dir.name)
    return directories_by_wxid


def _current_session_targets(raw_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    candidates: dict[str, set[str]] = {}
    issues: dict[str, str] = {}
    if not raw_root.is_dir():
        return {}, issues
    for session_dir in sorted(path for path in raw_root.iterdir() if path.is_dir()):
        for json_path in sorted(session_dir.glob("*.json")):
            payload = _read_session_payload(json_path)
            if payload is None:
                continue
            session = payload.get("session")
            if not isinstance(session, dict):
                continue
            wxid = str(session.get("wxid") or "").strip()
            display_name = str(session.get("displayName") or "").strip()
            session_type = str(session.get("type") or "").strip()
            if not wxid or not display_name or session_type not in {"私聊", "群聊"}:
                continue
            expected = f"{session_type}_{sanitize_session_name(display_name, fallback=wxid)}"
            if strip_date_suffix(session_dir.name) != expected:
                issues[wxid] = "本轮 raw 目录与 session.displayName 不一致，未猜测新目录"
                continue
            candidates.setdefault(wxid, set()).add(expected)
    targets: dict[str, str] = {}
    for wxid, names in candidates.items():
        if len(names) == 1:
            targets[wxid] = next(iter(names))
        else:
            issues[wxid] = "本轮导出给出多个 displayName，未猜测新目录"
    return targets, issues


def _read_session_payload(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _merge_session_directories(
    archive_root: Path,
    *,
    wxid: str,
    target_directory: str,
    source_directories: tuple[str, ...],
    find_guard_failures: Callable[[list[tuple[Path, Path]]], list[Any]],
    files_equal: Callable[[Path, Path], bool],
) -> SessionRenameMerge:
    target_root = archive_root / target_directory
    pairs: list[tuple[Path, Path]] = []
    rejected: dict[Path, str] = {}
    for source_directory in source_directories:
        source_root = archive_root / source_directory
        for source_path in sorted(path for path in source_root.rglob("*") if path.is_file()):
            destination = target_root / source_path.relative_to(source_root)
            if destination.exists() and not destination.is_file():
                rejected[source_path] = "目标同路径不是文件，已保留原文件"
                continue
            pairs.append((source_path, destination))

    for failure in find_guard_failures(pairs):
        rejected[Path(failure.source)] = str(failure.reason)

    moved = 0
    duplicates = 0
    for source_path, destination in pairs:
        if source_path in rejected:
            continue
        if destination.is_file() and files_equal(source_path, destination):
            _remove_file(source_path)
            duplicates += 1
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        _move_file(source_path, destination)
        moved += 1

    for source_directory in source_directories:
        _remove_empty_directories(archive_root / source_directory)

    relative_rejections = tuple(
        sorted(
            (
                (str(source_path.relative_to(archive_root)), reason)
                for source_path, reason in rejected.items()
            ),
            key=lambda item: item[0],
        )
    )
    return SessionRenameMerge(
        wxid=wxid,
        target_directory=target_directory,
        source_directories=source_directories,
        moved_files=moved,
        duplicate_files=duplicates,
        rejected_files=relative_rejections,
    )


def _move_file(source: Path, destination: Path) -> None:
    try:
        os.replace(source, destination)
    except OSError:
        if destination.exists():
            os.chmod(destination, stat.S_IREAD | stat.S_IWRITE)
        try:
            os.replace(source, destination)
        except OSError:
            shutil.copy2(source, destination)
            _remove_file(source)


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        path.unlink()


def _remove_empty_directories(root: Path) -> None:
    if not root.exists():
        return
    for directory in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


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
    "SessionRenameMerge",
    "SessionRenameReconciliation",
    "SessionRenameStay",
    "inspect_archive_session_names",
    "reconcile_archive_session_names",
    "update_session_rename_alarm",
    "write_session_rename_report",
]
