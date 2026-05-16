from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
import shutil

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
      * ``archive`` — move current contents to ``<rotation_root>/<yyyymmdd-HHMMSS>[-label]/{raw,processed}/``.
        Reversible. Used for manual / test reruns.
      * ``delete`` — ``shutil.rmtree`` the contents. Used in the daily cron once yesterday's data
        has already been promoted to ``WeFlow-archived-exports/``.
      * ``skip`` — leave both roots untouched (still ensures they exist).
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
            shutil.rmtree(source, ignore_errors=True)
            source.mkdir(parents=True, exist_ok=True)
        for path in candidates.values():
            path.mkdir(parents=True, exist_ok=True)
        return RotationResult(target=None, moved={}, mode=mode)

    # mode == "archive"
    stamp = (timestamp or datetime.now()).strftime("%Y%m%d-%H%M%S")
    folder_name = f"{stamp}-{label}" if label else stamp
    rotation_target = cfg.paths.rotation_root / folder_name
    rotation_target.mkdir(parents=True, exist_ok=True)
    moved: dict[str, Path] = {}
    for key, source in populated.items():
        destination = rotation_target / key
        shutil.move(str(source), str(destination))
        source.mkdir(parents=True, exist_ok=True)
        moved[key] = destination

    for path in candidates.values():
        path.mkdir(parents=True, exist_ok=True)
    return RotationResult(target=rotation_target, moved=moved, mode=mode)


def _is_non_empty_dir(path: Path) -> bool:
    return path.exists() and path.is_dir() and any(path.iterdir())
