from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import os
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_diary_core.archiving import strip_date_suffix
from wechat_diary_core.config import load_config
from wechat_diary_core.raw_schema import RawSchemaError, validate_moments_json, validate_session_json
from wechat_diary_core.workspace import merge_raw_exports_into_archive, merge_tree
from wechat_diary_core.workspace_discovery import WorkspaceResolutionError, resolve_config_path


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

    parser = argparse.ArgumentParser(
        description=(
            "Merge existing WeFlow export trees (e.g. backfill dirs like "
            "WeFlow-raw-exports-去年) into the long-term archive library at "
            "[paths].archived. Per-session, same-name files: newer wins."
        )
    )
    parser.add_argument("--config", default=None, help="Path to the local config file.")
    parser.add_argument("--raw-root", default="", help="A raw export tree to ingest into archived/raw/.")
    parser.add_argument(
        "--processed-root", default="", help="A processed tree to ingest into archived/processed/."
    )
    source_mode = parser.add_mutually_exclusive_group()
    source_mode.add_argument(
        "--move-source",
        action="store_true",
        help="Delete successfully ingested source trees. Without this flag, sources are kept.",
    )
    source_mode.add_argument(
        "--keep-source",
        action="store_true",
        help="Compatibility no-op: copy and keep the source tree (now the default).",
    )
    parser.add_argument(
        "--force-overwrite",
        action="store_true",
        help=(
            "Bypass only collisions whose age cannot be determined from file contents, after independently "
            "confirming the incoming snapshot is newer. Evidence-backed regressions and raw schema failures "
            "are never bypassed."
        ),
    )
    args = parser.parse_args(argv)

    if not args.raw_root and not args.processed_root:
        parser.error("Pass at least one of --raw-root / --processed-root.")

    try:
        config_path = resolve_config_path(args.config)
    except WorkspaceResolutionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    cfg = load_config(config_path)
    move = args.move_source
    raw_validation_failures: list[tuple[Path, str]] = []

    sources: list[tuple[str, Path]] = []
    for label, source_arg in (("raw", args.raw_root), ("processed", args.processed_root)):
        if not source_arg:
            continue
        source = Path(source_arg)
        if not source.is_absolute():
            source = (cfg.base_dir / source).resolve()
        if not source.is_dir():
            print(f"FAILED: {label} root is not a directory: {source}", file=sys.stderr)
            return 1
        sources.append((label, source))

    invalid_paths: set[Path] = set()
    raw_source = next((source for label, source in sources if label == "raw"), None)
    if raw_source is not None:
        raw_validation_failures = _validate_raw_export_tree(raw_source)
        invalid_paths = {path.resolve() for path, _reason in raw_validation_failures}
        for failed_path, reason in raw_validation_failures:
            print(f"RAW SCHEMA VALIDATION FAILED: {failed_path}", file=sys.stderr)
            print(f"  {reason}", file=sys.stderr)

    guard_failures: list[GuardFailure] = []
    for label, source in sources:
        destination = cfg.paths.archived / label
        pairs = (
            _raw_ingest_pairs(source, destination, invalid_paths)
            if label == "raw"
            else _tree_ingest_pairs(source, destination)
        )
        guard_failures.extend(_find_guard_failures(pairs))

    blocking_guard_failures = [
        failure
        for failure in guard_failures
        if not (args.force_overwrite and failure.force_eligible)
    ]
    if guard_failures:
        _report_guard_failures(
            guard_failures,
            force_requested=args.force_overwrite,
            blocking_count=len(blocking_guard_failures),
        )
        if blocking_guard_failures:
            return 1

    for label, source in sources:
        if label == "raw":
            if raw_validation_failures:
                count = _copy_raw_exports_except(source, cfg.paths.archived / "raw", invalid_paths)
            else:
                count = merge_raw_exports_into_archive(
                    source,
                    cfg.paths.archived / "raw",
                    move=move,
                )
        else:
            count = merge_tree(source, cfg.paths.archived / "processed", move=move)
        print(f"Merged {count} {label} files into {cfg.paths.archived / label}")
        if move and not (label == "raw" and raw_validation_failures):
            # Moving leaves empty directory shells behind; drop the whole source.
            shutil.rmtree(source, ignore_errors=True)
            print(f"Removed emptied source: {source}")
        elif move and label == "raw" and raw_validation_failures:
            print(f"Kept raw source for schema-failure review: {source}")

    if raw_validation_failures:
        print("\nRaw schema validation failures:", file=sys.stderr)
        for failed_path, reason in raw_validation_failures:
            print(f"- {failed_path}: {reason}", file=sys.stderr)
        return 1

    return 0


def _raw_ingest_pairs(
    source: Path,
    archive_raw_root: Path,
    invalid_paths: set[Path],
) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for entry in sorted(source.iterdir()):
        if entry.is_dir():
            destination_root = archive_raw_root / strip_date_suffix(entry.name)
            for path in sorted(entry.rglob("*")):
                if path.is_file() and path.resolve() not in invalid_paths:
                    pairs.append((path, destination_root / path.relative_to(entry)))
            continue
        if entry.resolve() not in invalid_paths:
            pairs.append((entry, archive_raw_root / entry.name))
    return pairs


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


def _files_equal(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_fh, right.open("rb") as right_fh:
        while True:
            left_chunk = left_fh.read(1024 * 1024)
            right_chunk = right_fh.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _collision_failure_reason(source: Path, destination: Path) -> tuple[str, bool] | None:
    if source.suffix.lower() != ".json" or destination.suffix.lower() != ".json":
        return "同路径非 JSON 文件内容不同，无法从文件内容证明 incoming 更新", True
    try:
        incoming = _read_json(source)
        archived = _read_json(destination)
    except (OSError, json.JSONDecodeError) as exc:
        return f"同路径 JSON 无法比较（{type(exc).__name__}）", True
    both_chat_json = _is_chat_json(incoming) and _is_chat_json(archived)
    if not both_chat_json:
        return "同路径非聊天 JSON 内容不同，没有可用的消息身份契约", True
    if incoming == archived:
        return None
    return _chat_regression_reason(incoming, archived)


def _read_json(path: Path) -> object:
    with path.open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def _is_chat_json(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("session"), dict)
        and isinstance(value.get("messages"), list)
    )


def _chat_regression_reason(incoming: dict, archived: dict) -> tuple[str, bool] | None:
    incoming_session = incoming["session"]
    archived_session = archived["session"]
    incoming_wxid = str(incoming_session.get("wxid") or "")
    archived_wxid = str(archived_session.get("wxid") or "")
    if incoming_wxid != archived_wxid:
        return (
            f"会话身份不一致（incoming wxid={incoming_wxid!r}, archived wxid={archived_wxid!r}）",
            False,
        )

    incoming_messages = incoming["messages"]
    archived_messages = archived["messages"]
    incoming_lineage = _chat_lineage(incoming)
    archived_lineage = _chat_lineage(archived)
    same_lineage = incoming_lineage is not None and incoming_lineage == archived_lineage

    if same_lineage:
        identity_label = "(createTime, localId)"
        incoming_identities = _local_identity_counter(incoming_messages)
        archived_identities = _local_identity_counter(archived_messages)
    else:
        identity_label = "platformMessageId"
        incoming_identities, incoming_missing = _platform_identity_counter(incoming_messages)
        archived_identities, archived_missing = _platform_identity_counter(archived_messages)
        if incoming_missing or archived_missing:
            return (
                "跨代消息身份不可比：platformMessageId 存在空值"
                f"（incoming={incoming_missing}, archived={archived_missing}）",
                True,
            )
        if not (incoming_identities & archived_identities):
            return (
                "跨代消息身份不可比：platformMessageId 零交集"
                f"（incoming lineage={incoming_lineage or 'unknown'}, "
                f"archived lineage={archived_lineage or 'unknown'}）",
                True,
            )

    incoming_max = _max_create_time(incoming_messages)
    archived_max = _max_create_time(archived_messages)
    if incoming_max < archived_max:
        return f"incoming 时间水位更旧（max createTime {incoming_max} < {archived_max}）", False

    missing_from_incoming = archived_identities - incoming_identities
    if missing_from_incoming:
        return (
            f"incoming 未覆盖 archived 的全部消息身份（按 {identity_label} 多重集合缺 "
            f"{sum(missing_from_incoming.values())} 条）",
            False,
        )

    shared_messages_changed = same_lineage and _shared_messages_changed(
        incoming_messages,
        archived_messages,
    )
    if same_lineage and incoming_lineage == "legacy_gui":
        incoming_exported_at = _legacy_exported_at(incoming)
        archived_exported_at = _legacy_exported_at(archived)
        if incoming_exported_at is not None and archived_exported_at is not None:
            if incoming_exported_at < archived_exported_at:
                return (
                    "incoming legacy 快照 exportedAt 更旧"
                    f"（{incoming_exported_at} < {archived_exported_at}）",
                    False,
                )
            if shared_messages_changed and incoming_exported_at > archived_exported_at:
                return None

    if shared_messages_changed:
        return "同代共同消息内容不同，缺少可信快照版本字段，无法证明 incoming 更新", True
    return None


def _chat_lineage(data: dict) -> str | None:
    metadata = data.get("weflow")
    if not isinstance(metadata, dict):
        return None
    source = str(metadata.get("source") or "").strip().casefold()
    if source == "http_api":
        return "http_api"
    generator = str(metadata.get("generator") or "").strip().casefold()
    if generator == "weflow":
        return "legacy_gui"
    return None


def _legacy_exported_at(data: dict) -> int | None:
    metadata = data.get("weflow")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("exportedAt")
    if isinstance(value, bool):
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if timestamp > 0 else None


def _local_identity_counter(messages: list) -> Counter[tuple[int, str]]:
    return Counter(
        (_numeric_create_time(message), str(message.get("localId")))
        for message in messages
        if isinstance(message, dict)
    )


def _platform_identity_counter(messages: list) -> tuple[Counter[str], int]:
    identities: Counter[str] = Counter()
    missing = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        value = message.get("platformMessageId")
        if value is None or str(value) == "":
            missing += 1
        else:
            identities[str(value)] += 1
    return identities, missing


def _max_create_time(messages: list) -> int:
    return max((_numeric_create_time(message) for message in messages if isinstance(message, dict)), default=0)


def _numeric_create_time(message: dict) -> int:
    try:
        return int(message.get("createTime") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _shared_messages_changed(incoming_messages: list, archived_messages: list) -> bool:
    incoming_by_identity = _messages_by_local_identity(incoming_messages)
    archived_by_identity = _messages_by_local_identity(archived_messages)
    for identity in incoming_by_identity.keys() & archived_by_identity.keys():
        if incoming_by_identity[identity] != archived_by_identity[identity]:
            return True
    return False


def _messages_by_local_identity(messages: list) -> dict[tuple[int, str], list[dict]]:
    grouped: dict[tuple[int, str], list[dict]] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        identity = (_numeric_create_time(message), str(message.get("localId")))
        grouped.setdefault(identity, []).append(message)
    return grouped


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


def _copy_raw_exports_except(source: Path, archive_raw_root: Path, invalid_paths: set[Path]) -> int:
    count = 0
    for entry in sorted(source.iterdir()):
        if entry.is_dir():
            count += _copy_tree_except(entry, archive_raw_root / strip_date_suffix(entry.name), invalid_paths)
            continue
        if entry.resolve() in invalid_paths:
            continue
        _copy_file_replacing(entry, archive_raw_root / entry.name)
        count += 1
    return count


def _copy_tree_except(source: Path, destination: Path, invalid_paths: set[Path]) -> int:
    count = 0
    for src in sorted(source.rglob("*")):
        if not src.is_file() or src.resolve() in invalid_paths:
            continue
        dst = destination / src.relative_to(source)
        _copy_file_replacing(src, dst)
        count += 1
    return count


def _copy_file_replacing(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
    except OSError:
        if dst.exists():
            os.chmod(dst, stat.S_IREAD | stat.S_IWRITE)
        shutil.copy2(src, dst)


if __name__ == "__main__":
    raise SystemExit(main())
