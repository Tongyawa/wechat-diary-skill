from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
import json
import os
import shutil
import stat

from .archiving import (chat_render_targets, chat_sidecar_targets, SELF_MOMENTS_SUBROOT,
                        normalize_target_subroot)
from .preprocessing.moments import MomentsExport, moments_render_targets
from .processed_reporting import (report_raw_result, read_report_text, update_processed_report,
                                  open_report_if_changed)
from .preprocessing import ProcessedChatExport
from .config import Config
from .media_store import MEDIA_STORE_NAME
from .raw_archive import (plan_raw_ingest, apply_raw_ingest_plan,
                          remove_empty_directories)


CleanupMode = Literal["archive", "delete", "skip"]


@dataclass(frozen=True)
class RotationResult:
    target: Path | None
    moved: dict[str, Path]
    mode: CleanupMode
    retained_sources: frozenset[Path] = frozenset()


def rotate_export_workspace(
    config: Config | None = None,
    label: str | None = None,
    timestamp: datetime | None = None,
    mode: CleanupMode = "archive",
    archive_headers: dict | None = None,
    report_opener=None,
) -> RotationResult:
    """Clear raw/processed roots before a fresh export run.

    Modes:
      * ``archive`` — plan raw chat targets by identity, publish verified bytes,
        and remove only consumed sources. Unsupported daily targets retain the
        historical name mapping with an advisory report. Processed keeps its
        relative paths under ``archived/processed/`` with incoming replacement.
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
    if mode == "archive" and "raw" not in populated:
        from .raw_isolation import report_without_raw
        report_without_raw(cfg.paths.archived, report_opener)

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
    retained_inputs: list[Path] = []
    previous_report = read_report_text(cfg.paths.archived)
    report_requests = []
    defer_report = report_requests.append if report_opener else None
    if "raw" in populated:
        destination = cfg.paths.archived / "raw"
        merge_raw_exports_into_archive(populated["raw"], destination, archive_headers=archive_headers,
                                       report_opener=defer_report, retained_inputs=retained_inputs)
        moved["raw"] = destination
    if "processed" in populated:
        destination = cfg.paths.archived / "processed"
        merge_processed_into_archive(populated['processed'], destination, retained_sources=retained_inputs,
                                     config=cfg, report_opener=defer_report)
        moved["processed"] = destination

    if report_requests:
        open_report_if_changed(cfg.paths.archived, previous_report, report_opener)

    # Publication and isolation own file removal; only remove empty shells here.
    if "processed" in populated:
        remove_empty_directories(populated['processed'])
    if "raw" in populated:
        remove_empty_directories(populated["raw"])

    for path in candidates.values():
        path.mkdir(parents=True, exist_ok=True)
    return RotationResult(target=cfg.paths.archived, moved=moved, mode=mode,
                          retained_sources=frozenset(retained_inputs))


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
    archive_headers: dict | None = None,
    report_opener=None,
    retained_inputs: list[Path] | None = None,
) -> int:
    """Archive through one daily plan, preserving every unconsumed source."""
    plan = plan_raw_ingest(archive_raw_root, [raw_root], policy="daily", header_cache=archive_headers)
    result = apply_raw_ingest_plan(plan, move_source=move, include_ranges=True)
    if retained_inputs is not None:
        retained_inputs.extend(result.isolated_sources.get(source, source) for source in result.retained)
    report_raw_result(plan, result, report_opener=report_opener)
    return len(result.published)


def _retained_exports(retained_sources):
    for source in retained_sources:
        source = Path(os.path.abspath(source))
        if source.suffix.lower() != '.json' or MEDIA_STORE_NAME in source.parts or not source.is_file():
            continue
        try:
            data = json.loads(source.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            yield source, data


def retained_chat_targets(retained_sources) -> set[Path]:
    targets = set()
    for source, data in _retained_exports(retained_sources):
        if isinstance(data.get('session'), dict) and isinstance(data.get('messages'), list):
            targets.update(chat_render_targets(ProcessedChatExport(source, source.parent.name, data)))
    return targets


def retained_multi_targets(retained_sources, config) -> dict[Path, set[Path]]:
    """Use the exact recipient/subroot parameters of the daily renderers."""
    targets = {}
    if config is None:
        return targets
    daily = config.daily_export
    subroot = normalize_target_subroot(daily.target_processed_subroot)
    for source, data in _retained_exports(retained_sources):
        outputs = []
        if isinstance(data.get('session'), dict) and isinstance(data.get('messages'), list):
            export = ProcessedChatExport(source, source.parent.name, data)
            outputs.extend(chat_sidecar_targets(export, daily.target_usernames, f'{subroot}/chats'))
        if isinstance(data.get('posts'), list):
            export = MomentsExport(source, data['posts'], data.get('filters') or {})
            if daily.self_moments_usernames:
                outputs.extend(moments_render_targets(export, daily.self_moments_usernames, SELF_MOMENTS_SUBROOT))
            if daily.target_usernames:
                outputs.extend(moments_render_targets(export, daily.target_usernames, f'{subroot}/moments'))
        for relative in outputs:
            targets.setdefault(relative, set()).add(source)
    return targets


@dataclass(frozen=True)
class ProcessedMergePlan:
    skipped_chat: frozenset[Path]
    skipped_multi: frozenset[Path]
    introduced_multi: dict[Path, set[Path]]


def plan_processed_merge(source, destination, *, retained_sources=(), config=None) -> ProcessedMergePlan:
    """Read-only B' decision; it never changes or replans raw ingestion."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    retained_sources = set(retained_sources)
    if retained_sources and config is None:
        raise ValueError('retained inputs require the renderer config for processed merging')
    one_to_one = retained_chat_targets(retained_sources)
    many_to_one = retained_multi_targets(retained_sources, config)
    skipped_chat, skipped_multi, introduced = set(), set(), {}
    for path in source.rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if relative in one_to_one:
            skipped_chat.add(relative)
        elif relative in many_to_one:
            if (destination / relative).exists():
                skipped_multi.add(relative)
            else:
                introduced[relative] = many_to_one[relative]
    return ProcessedMergePlan(frozenset(skipped_chat), frozenset(skipped_multi), introduced)


def merge_processed_into_archive(source, destination, *, move=True, retained_sources=(), config=None, report_opener=None) -> int:
    """Leave skipped files live and unprotected; clear_first removes them later."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    decision = plan_processed_merge(source, destination, retained_sources=retained_sources, config=config)
    skipped = decision.skipped_chat | decision.skipped_multi
    count = 0
    introduced = {}
    for path in sorted(source.rglob('*')):
        if path.is_file():
            relative = path.relative_to(source)
            if relative in skipped:
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _replace_file(path, target, move=move)
            count += 1
            if relative in decision.introduced_multi:
                introduced[relative] = decision.introduced_multi[relative]
    previous = read_report_text(destination.parent)
    changed = update_processed_report(destination.parent, skipped_chat=decision.skipped_chat,
                                      skipped_multi=decision.skipped_multi, introduced=introduced)
    if changed:
        open_report_if_changed(destination.parent, previous, report_opener)
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
