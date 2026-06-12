from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
import os
import shutil
import stat

from .archiving import strip_date_suffix
from .config import Config, load_config


CleanupMode = Literal["archive", "delete", "skip"]


@dataclass(frozen=True)
class RotationResult:
    target: Path | None
    moved: dict[str, Path]
    mode: CleanupMode


def rotate_export_workspace(
    config: Config | None = None,
    label: str | None = None,
    timestamp: datetime | None = None,
    mode: CleanupMode = "archive",
) -> RotationResult:
    """Clear raw/processed roots before a fresh export run.

    Modes:
      * ``archive`` — merge current contents into the long-term library under
        ``paths.archived``: raw session folders lose their date suffix and land
        in ``archived/raw/<会话>/``, processed keeps its layout under
        ``archived/processed/<会话>/<day>.md``. Same relative path = the newer
        file replaces the archived one, so repeated ingestion deduplicates.
      * ``delete`` — ``shutil.rmtree`` the contents, no archiving.
      * ``skip`` — leave both roots untouched (still ensures they exist).

    ``label`` / ``timestamp`` are accepted for backwards compatibility; the
    merge-based archive no longer creates timestamped snapshot folders.
    """
    cfg = config or load_config()
    candidates = {"raw": cfg.paths.raw, "processed": cfg.paths.processed}

    if mode == "skip":
        for path in candidates.values():
            path.mkdir(parents=True, exist_ok=True)
        return RotationResult(target=None, moved={}, mode=mode)

    populated = {key: path for key, path in candidates.items() if _is_non_empty_dir(path)}

    if not populated:
        for path in candidates.values():
            path.mkdir(parents=True, exist_ok=True)
        return RotationResult(target=None, moved={}, mode=mode)

    if mode == "delete":
        for source in populated.values():
            _remove_tree(source)
            source.mkdir(parents=True, exist_ok=True)
        for path in candidates.values():
            path.mkdir(parents=True, exist_ok=True)
        return RotationResult(target=None, moved={}, mode=mode)

    # mode == "archive"
    moved: dict[str, Path] = {}
    if "raw" in populated:
        destination = cfg.paths.archived / "raw"
        merge_raw_exports_into_archive(populated["raw"], destination)
        moved["raw"] = destination
    if "processed" in populated:
        destination = cfg.paths.archived / "processed"
        merge_tree(populated["processed"], destination)
        moved["processed"] = destination

    for source in populated.values():
        _remove_tree(source)

    for path in candidates.values():
        path.mkdir(parents=True, exist_ok=True)
    return RotationResult(target=cfg.paths.archived, moved=moved, mode=mode)


def merge_tree(source: str | Path, destination: str | Path, *, move: bool = True) -> int:
    """File-level merge of ``source`` into ``destination``.

    Every file keeps its relative path; an existing file at the same path is
    replaced (the incoming version wins). Returns the number of files merged.
    With ``move=True`` files are renamed away (same-volume = instant) and the
    emptied directory shells are left for the caller to clean up.
    """
    src_root = Path(source)
    dst_root = Path(destination)
    count = 0
    for src in sorted(src_root.rglob("*")):
        if not src.is_file():
            continue
        dst = dst_root / src.relative_to(src_root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        _replace_file(src, dst, move=move)
        count += 1
    return count


def merge_raw_exports_into_archive(
    raw_root: str | Path,
    archive_raw_root: str | Path,
    *,
    move: bool = True,
) -> int:
    """Merge a WeFlow raw export tree into the per-session archive library.

    Top-level session folders lose their ``_YYYYMMDD`` / ``_YYYYMMDD-YYYYMMDD``
    suffix so every export of the same chat accumulates in one folder. The
    files inside keep their (date-suffixed) names, so different days coexist
    and a re-export of the same day overwrites the older copy. Root-level
    non-session entries (moments json, the shared ``media/`` dir) merge under
    their own names.
    """
    source = Path(raw_root)
    target_root = Path(archive_raw_root)
    count = 0
    for entry in sorted(source.iterdir()):
        if entry.is_dir():
            count += merge_tree(entry, target_root / strip_date_suffix(entry.name), move=move)
        else:
            target_root.mkdir(parents=True, exist_ok=True)
            _replace_file(entry, target_root / entry.name, move=move)
            count += 1
    return count


def _replace_file(src: Path, dst: Path, *, move: bool = True) -> None:
    try:
        if move:
            os.replace(src, dst)
        else:
            shutil.copy2(src, dst)
    except OSError:
        # Windows refuses to overwrite read-only files (WeFlow media often is).
        if dst.exists():
            os.chmod(dst, stat.S_IREAD | stat.S_IWRITE)
        if move:
            try:
                os.replace(src, dst)
            except OSError:
                # Cross-device fallback: copy then remove the source.
                shutil.copy2(src, dst)
                os.chmod(src, stat.S_IREAD | stat.S_IWRITE)
                src.unlink()
        else:
            shutil.copy2(src, dst)


def _is_non_empty_dir(path: Path) -> bool:
    return path.exists() and path.is_dir() and any(path.iterdir())


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, onerror=_handle_remove_error)


def _handle_remove_error(function, path: str, _exc_info) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    function(path)
