from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_diary_core.archiving import archive, archive_chats_for
from wechat_diary_core.config import Config, load_config
from wechat_diary_core.preprocessing import archive_moments_for
from wechat_diary_core.workspace import merge_tree


DAY_SUFFIX_RE = re.compile(r"_(\d{8})$")
RANGE_SUFFIX_RE = re.compile(r"_(\d{8})-(\d{8})$")


@dataclass(frozen=True)
class ExistingRawProcessResult:
    raw_root: Path
    day: str | None
    processed_backup: Path | None
    diary_files: list[Path]
    self_moment_files: list[Path]
    sidecar_chat_files: list[Path]
    sidecar_moment_files: list[Path]


@dataclass
class ProcessExistingRawDeps:
    archive: Callable[..., Any] = archive
    archive_chats_for: Callable[..., Any] = archive_chats_for
    archive_moments_for: Callable[..., Any] = archive_moments_for
    run_voice_fallback_script: Callable[..., Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.run_voice_fallback_script is None:
            self.run_voice_fallback_script = run_voice_fallback_script_for_raw


class ProcessExistingRawStageError(RuntimeError):
    def __init__(self, stage: str, cause: BaseException) -> None:
        self.stage = stage
        self.cause = cause
        super().__init__(f"{stage}: {cause}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Process existing WeFlow raw exports without starting WeFlow.")
    parser.add_argument("--config", default="config.toml", help="Path to the local config file.")
    parser.add_argument("--raw-root", default="", help="Existing raw export root. Defaults to config [paths].raw.")
    parser.add_argument("--day", default="", help="Day for downstream skills, yyyy-mm-dd. Auto-inferred for single-day raw.")
    parser.add_argument(
        "--require-day",
        action="store_true",
        help="Fail if --day is omitted and the raw root does not have one unambiguous day suffix.",
    )
    parser.add_argument(
        "--skip-voice-fallback",
        action="store_true",
        help="Do not run [daily_export].voice_fallback_script before processing raw.",
    )
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = ROOT / config_path

    try:
        cfg = load_config(config_path)
        result = process_existing_raw(
            cfg,
            raw_root=args.raw_root or None,
            day=args.day or None,
            require_day=args.require_day,
            skip_voice_fallback=args.skip_voice_fallback,
        )
    except ProcessExistingRawStageError as exc:
        print(f"\nFAILED at stage: {exc.stage}", file=sys.stderr)
        print(f"Reason: {exc.cause}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nFAILED before raw processing completed: {exc}", file=sys.stderr)
        return 1

    print("\nExisting raw processing completed.")
    print(f"Raw root: {result.raw_root}")
    print(f"Day: {result.day or 'not inferred; pass --day for skill summaries'}")
    print(f"Processed backup: {result.processed_backup or 'none'}")
    print(f"Diary processed files: {len(result.diary_files)}")
    print(f"Self moments files: {len(result.self_moment_files)}")
    print(f"Sidecar chat files: {len(result.sidecar_chat_files)}")
    print(f"Sidecar moments files: {len(result.sidecar_moment_files)}")
    for path in result.diary_files + result.self_moment_files + result.sidecar_chat_files + result.sidecar_moment_files:
        print(f"- {path}")
    return 0


def process_existing_raw(
    cfg: Config,
    *,
    raw_root: str | Path | None = None,
    day: str | None = None,
    require_day: bool = False,
    skip_voice_fallback: bool = False,
    deps: ProcessExistingRawDeps | None = None,
    timestamp: datetime | None = None,
) -> ExistingRawProcessResult:
    active_deps = deps or ProcessExistingRawDeps()
    source = _resolve_input_path(cfg, raw_root) if raw_root is not None else cfg.paths.raw
    if not source.exists():
        raise FileNotFoundError(f"Raw root does not exist: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"Raw root is not a directory: {source}")

    resolved_day = _normalize_day(day) if day else infer_single_day(source)
    if require_day and resolved_day is None:
        raise ValueError(
            "Could not infer one unambiguous day from raw folder suffixes. "
            "Pass --day yyyy-mm-dd for range or multi-day raw exports."
        )

    print(f"Process existing raw root: {source}")
    print(f"Processed root: {cfg.paths.processed}")
    print(f"Day: {resolved_day or 'not inferred'}")

    backup = _run_stage(
        "archive_existing_processed",
        lambda: archive_existing_processed(cfg.paths.processed, cfg.paths.archived / "processed"),
    )

    if cfg.daily_export.voice_fallback_script and not skip_voice_fallback:
        _run_stage(
            "voice_fallback",
            lambda: active_deps.run_voice_fallback_script(cfg.daily_export.voice_fallback_script, cfg, source),
        )
    elif cfg.daily_export.voice_fallback_script:
        print("voice_fallback skipped: --skip-voice-fallback was set.")
    else:
        print("voice_fallback skipped: no configured script.")

    diary_files = _run_stage(
        "archive_diary_processed",
        lambda: active_deps.archive(source, config=cfg, clear_first=True),
    )

    self_moment_files: list[Path] = []
    self_moments_usernames = list(cfg.daily_export.self_moments_usernames)
    if self_moments_usernames:
        self_moment_files = _run_stage(
            "archive_self_moments",
            lambda: active_deps.archive_moments_for(
                self_moments_usernames,
                raw_path=source,
                config=cfg,
                subroot="朋友圈_自己",
                clear_first=True,
            ),
        )
        _ensure_day_file_if_needed(cfg.paths.processed / "朋友圈_自己", resolved_day, self_moment_files)

    target_usernames = list(cfg.daily_export.target_usernames)
    subroot = _normalize_subroot(cfg.daily_export.target_processed_subroot)
    sidecar_chat_files: list[Path] = []
    sidecar_moment_files: list[Path] = []
    if target_usernames:
        sidecar_chat_files = _run_stage(
            "archive_target_chats",
            lambda: active_deps.archive_chats_for(
                target_usernames,
                raw_path=source,
                config=cfg,
                subroot=f"{subroot}/chats",
                image_mode="preserve_paths",
                clear_first=True,
            ),
        )
        _ensure_day_file_if_needed(cfg.paths.processed / subroot / "chats", resolved_day, sidecar_chat_files)
        sidecar_moment_files = _run_stage(
            "archive_target_moments",
            lambda: active_deps.archive_moments_for(
                target_usernames,
                raw_path=source,
                config=cfg,
                subroot=f"{subroot}/moments",
                clear_first=True,
            ),
        )
        _ensure_day_file_if_needed(cfg.paths.processed / subroot / "moments", resolved_day, sidecar_moment_files)

    return ExistingRawProcessResult(
        raw_root=source,
        day=resolved_day,
        processed_backup=backup,
        diary_files=list(diary_files),
        self_moment_files=list(self_moment_files),
        sidecar_chat_files=list(sidecar_chat_files),
        sidecar_moment_files=list(sidecar_moment_files),
    )


def infer_single_day(raw_root: str | Path) -> str | None:
    root = Path(raw_root)
    days: set[str] = set()
    has_range = False
    if not root.exists():
        return None
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if RANGE_SUFFIX_RE.search(child.name):
            has_range = True
            continue
        match = DAY_SUFFIX_RE.search(child.name)
        if match:
            days.add(_format_day(match.group(1)))
    if len(days) == 1 and not has_range:
        return next(iter(days))
    return None


def archive_existing_processed(
    processed_root: str | Path,
    archived_processed_root: str | Path,
) -> Path | None:
    """Merge the current processed tree into the long-term archive library.

    Same relative path = the incoming file replaces the archived one, so
    re-processing a day never duplicates and always keeps the newest render.
    """
    source = Path(processed_root)
    if not _is_non_empty_dir(source):
        source.mkdir(parents=True, exist_ok=True)
        return None

    target = Path(archived_processed_root)
    merge_tree(source, target)
    shutil.rmtree(source, onerror=_handle_remove_error)
    source.mkdir(parents=True, exist_ok=True)
    return target


def run_voice_fallback_script_for_raw(script_path: str | Path, cfg: Config, raw_root: str | Path) -> None:
    script = Path(script_path)
    if not script.exists():
        raise FileNotFoundError(f"Voice fallback script does not exist: {script}")

    target_runs = list(cfg.daily_export.target_usernames) or [""]
    for target in target_runs:
        command = [sys.executable, str(script), "--raw-root", str(raw_root)]
        if target:
            command.extend(["--target-wxid", target])
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        subprocess.run(command, cwd=cfg.base_dir, env=env, check=True)


def _ensure_day_file_if_needed(root: Path, day: str | None, written: list[Path]) -> None:
    if day is None:
        return
    expected = root / f"{day}.md"
    if expected in written or expected.exists():
        if expected not in written:
            written.append(expected)
        return
    root.mkdir(parents=True, exist_ok=True)
    expected.write_text("", encoding="utf-8")
    written.append(expected)


def _resolve_input_path(cfg: Config, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (cfg.base_dir / path).resolve()


def _normalize_day(value: str) -> str:
    text = value.strip()
    if re.fullmatch(r"\d{8}", text):
        return _format_day(text)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    raise ValueError(f"Invalid day format: {value!r}; expected yyyy-mm-dd or yyyymmdd.")


def _format_day(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def _normalize_subroot(value: str) -> str:
    cleaned = value.strip().strip("/\\")
    return cleaned or "_targets"


def _is_non_empty_dir(path: Path) -> bool:
    return path.exists() and path.is_dir() and any(path.iterdir())


def _handle_remove_error(function, path: str, _exc_info) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    function(path)


def _run_stage(stage: str, action: Callable[[], Any]) -> Any:
    print(f"[{datetime.now():%H:%M:%S}] {stage}...")
    try:
        result = action()
    except Exception as exc:
        raise ProcessExistingRawStageError(stage, exc) from exc
    print(f"[{datetime.now():%H:%M:%S}] {stage} done.")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
