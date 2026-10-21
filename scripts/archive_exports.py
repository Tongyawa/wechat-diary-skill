from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_diary_core.archiving import strip_date_suffix
from wechat_diary_core.config import load_config
from wechat_diary_core.media_store import MEDIA_SCHEME_KEY, MEDIA_SCHEME_VALUE, MEDIA_STORE_NAME
from wechat_diary_core.moments_ranges import REVIEW_EXIT_CODE, range_digest
from wechat_diary_core.raw_schema import RawSchemaError, validate_moments_json, validate_session_json
from wechat_diary_core.workspace import merge_tree
from wechat_diary_core.processed_reporting import report_raw_result
from wechat_diary_core.raw_archive import (
    plan_raw_ingest, apply_raw_ingest_plan, reject_overlapping_roots,
    remove_empty_directories, _fingerprint, _unlink,
)
from wechat_diary_core.workspace_discovery import WorkspaceResolutionError, resolve_config_path


# Compatibility imports; the policy and identity counters have one core implementation.
from wechat_diary_core.chat_identity import (
    _chat_regression_reason, _chat_lineage, _legacy_exported_at,
    _local_identity_counter, _platform_identity_counter, _max_create_time,
    _numeric_create_time, _shared_messages_changed, _messages_by_local_identity,
    _files_equal, _collision_failure_reason, _read_json, _is_chat_json,
)

MAX_REPORTED_GUARD_FAILURES = 12


@dataclass(frozen=True)
class GuardFailure:
    source: Path
    destination: Path
    reason: str
    force_eligible: bool


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Plan or ingest raw exports into the archive.")
    parser.add_argument("--config", default=None, help="Path to the local config file.")
    parser.add_argument("--raw-root", action="append", default=[], help="Raw source root; repeat for one batch.")
    parser.add_argument("--processed-root", default="", help="Processed source root.")
    parser.add_argument("--print-plan", action="store_true", help="Read-only JSON plan; never writes files.")
    parser.add_argument("--archive-raw-root", default="", help="Explicit archive raw root for --print-plan.")
    source_mode = parser.add_mutually_exclusive_group()
    source_mode.add_argument("--move-source", action="store_true", help="Remove only successfully ingested sources.")
    source_mode.add_argument("--keep-source", action="store_true", help="Compatibility no-op; keep sources by default.")
    parser.add_argument("--force-overwrite", action="store_true", help="Confirm only age-unknown collisions; never bypass hard guards.")
    args = parser.parse_args(argv)
    if not args.raw_root and not args.processed_root:
        parser.error("Pass at least one of --raw-root / --processed-root.")
    if args.print_plan and (args.move_source or args.force_overwrite):
        parser.error("--print-plan cannot be combined with --move-source / --force-overwrite")
    if args.archive_raw_root and not args.print_plan:
        parser.error("--archive-raw-root requires --print-plan")
    if args.print_plan and args.processed_root:
        parser.error("--print-plan describes raw ingestion; omit --processed-root")
    try:
        if args.archive_raw_root:
            cfg = None
            base = Path.cwd()
            archive_raw = Path(args.archive_raw_root).resolve()
        else:
            cfg = load_config(resolve_config_path(args.config))
            base = cfg.base_dir
            archive_raw = (cfg.paths.archived / "raw").resolve()
        raw_roots = [(base / Path(p)).resolve() for p in args.raw_root]
        processed = (base / args.processed_root).resolve() if args.processed_root else None
        sources = raw_roots + ([processed] if processed else [])
        reject_overlapping_roots(archive_raw.parent, sources)
        if any(not p.is_dir() for p in sources):
            raise ValueError("source root is not a directory")
        plan = plan_raw_ingest(archive_raw, raw_roots) if raw_roots else None
    except WorkspaceResolutionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    if args.print_plan:
        print(json.dumps(plan.to_dict(), ensure_ascii=False))
        return 1 if any(plan.summary[k] for k in ("resolutionBlocked", "hardBlocked", "schemaInvalid", "coverageFailed")) else 0

    raw_validation_failures = plan.schema_failures if plan else []
    for failure in raw_validation_failures:
        print(f"RAW SCHEMA VALIDATION FAILED: {failure['path']}\n  {failure['reason']}", file=sys.stderr)
    guard_failures = []
    if plan:
        for action in plan.actions:
            for guard in action.guards:
                guard_failures.append(GuardFailure(action.source, archive_raw / guard["path"],
                                                   guard["reason"], guard["forceEligible"]))
            if action.status != "ready":
                guard_failures.append(GuardFailure(action.source, action.target,
                                                   "; ".join(action.reasons), False))
    if processed:
        # Reject the whole supplied batch before any raw/processed/report write,
        # including every supplied processed file.
        guard_failures.extend(_find_guard_failures(_tree_ingest_pairs(processed, archive_raw.parent / 'processed')))
    blocked = [f for f in guard_failures if not (args.force_overwrite and f.force_eligible)]
    if guard_failures:
        _report_guard_failures(guard_failures, force_requested=args.force_overwrite, blocking_count=len(blocked))
        if blocked:
            return 1
    range_review = False
    if plan:
        ranges = [a for a in plan.actions if a.kind == "range"]
        # Range retirement can require review; keep all input until that is known.
        range_review = any(a.range_review for a in plan.actions)
        move_now = args.move_source and not raw_validation_failures and not ranges
        try:
            result = apply_raw_ingest_plan(plan, move_source=move_now, force_overwrite=args.force_overwrite)
            range_review = result.range_review
            report_raw_result(plan, result)
            for action in ranges:
                for warning in action.warnings:
                    print(f'[WARN] 朋友圈区间待核对：{warning}')
            if args.move_source and not raw_validation_failures and not range_review and ranges:
                source_hashes = {a.source: a.source_sha for a in plan.actions}
                for source in result.consumed:
                    if source.exists():
                        if _fingerprint(source) != source_hashes[source]:
                            raise ValueError('source changed before removal')
                        _unlink(source)
            for root in raw_roots:
                if args.move_source and not raw_validation_failures and not range_review:
                    remove_empty_directories(root, remove_root=True)
                elif args.move_source:
                    print(f"Kept raw source for validation/replacement review: {root}")
            print(f"Merged {len(result.published)} raw files into {archive_raw}")
        except (OSError, ValueError) as exc:
            print(f"FAILED: raw publication: {exc}", file=sys.stderr)
            return 1
    if processed:
        # All collision rejection already happened before the first write.
        try:
            count = merge_tree(processed, archive_raw.parent / "processed", move=args.move_source)
        except (OSError, ValueError) as exc:
            print(f"FAILED: processed merge: {exc}", file=sys.stderr)
            return 1
        if args.move_source:
            remove_empty_directories(processed, remove_root=True)
        print(f"Merged {count} processed files into {archive_raw.parent / 'processed'}")
    if raw_validation_failures:
        return 1
    return REVIEW_EXIT_CODE if range_review else 0


def _tree_ingest_pairs(source: Path, destination: Path) -> list[tuple[Path, Path]]:
    return [
        (path, destination / path.relative_to(source))
        for path in sorted(source.rglob("*"))
        if path.is_file()
    ]


def _find_guard_failures(pairs: list[tuple[Path, Path]]) -> list[GuardFailure]:
    failures: list[GuardFailure] = []
    for source, destination in pairs:
        if not destination.is_file() or _files_equal(source, destination):
            continue
        decision = _collision_failure_reason(source, destination)
        if decision is not None:
            reason, force_eligible = decision
            failures.append(GuardFailure(source, destination, reason, force_eligible))
    return failures


def _report_guard_failures(
    failures: list[GuardFailure],
    *,
    force_requested: bool,
    blocking_count: int,
) -> None:
    overridden_count = len(failures) - blocking_count
    heading = "OVERWRITE GUARD REJECTED" if blocking_count else "OVERWRITE GUARD OVERRIDDEN"
    print(
        f"{heading}: {len(failures)} differing destination file(s); "
        f"blocking={blocking_count}, override-eligible={overridden_count}.",
        file=sys.stderr,
    )
    for failure in failures[:MAX_REPORTED_GUARD_FAILURES]:
        print(f"- incoming: {failure.source}", file=sys.stderr)
        print(f"  archived: {failure.destination}", file=sys.stderr)
        print(f"  reason: {failure.reason}", file=sys.stderr)
        classification = "机器无法判定，可显式确认" if failure.force_eligible else "已有回退证据，禁止强制覆盖"
        print(f"  class: {classification}", file=sys.stderr)
    hidden = len(failures) - MAX_REPORTED_GUARD_FAILURES
    if hidden > 0:
        print(f"- ... {hidden} additional conflict(s) not shown.", file=sys.stderr)
    if blocking_count:
        if force_requested and overridden_count:
            print(
                "No files were written: --force-overwrite accepted only the unverifiable conflicts, "
                "but evidence-backed regressions remain blocked.",
                file=sys.stderr,
            )
        else:
            print("No files were written.", file=sys.stderr)
        print(
            "Ingest snapshots oldest-to-newest. --force-overwrite only bypasses conflicts whose age "
            "cannot be determined; it never bypasses a strict subset, older time watermark, or "
            "session identity mismatch.",
            file=sys.stderr,
        )
    else:
        print(
            "Proceeding because --force-overwrite was explicitly supplied after independent "
            "confirmation that incoming is newer.",
            file=sys.stderr,
        )


def _validate_raw_export_tree(source: Path) -> list[tuple[Path, str]]:
    failures: list[tuple[Path, str]] = []
    for path in sorted(source.rglob("*.json")):
        try:
            with path.open("r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("session"), dict) and isinstance(data.get("messages"), list):
                validate_session_json(data)
            elif isinstance(data, dict) and isinstance(data.get("posts"), list):
                validate_moments_json(data)
            else:
                raise RawSchemaError(
                    "not a canonical raw chat or moments JSON; expected session/messages or posts"
                )
        except (OSError, json.JSONDecodeError, RawSchemaError) as exc:
            failures.append((path, str(exc)))
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
