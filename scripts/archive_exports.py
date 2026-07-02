from __future__ import annotations

import argparse
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
    parser.add_argument("--config", default="config.toml", help="Path to the local config file.")
    parser.add_argument("--raw-root", default="", help="A raw export tree to ingest into archived/raw/.")
    parser.add_argument(
        "--processed-root", default="", help="A processed tree to ingest into archived/processed/."
    )
    parser.add_argument(
        "--keep-source",
        action="store_true",
        help="Copy instead of move; the source tree is left untouched.",
    )
    args = parser.parse_args(argv)

    if not args.raw_root and not args.processed_root:
        parser.error("Pass at least one of --raw-root / --processed-root.")

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (Path.cwd() / config_path).resolve()
    cfg = load_config(config_path)
    move = not args.keep_source
    raw_validation_failures: list[tuple[Path, str]] = []

    for label, source_arg, merge in (
        ("raw", args.raw_root, lambda src: merge_raw_exports_into_archive(src, cfg.paths.archived / "raw", move=move)),
        ("processed", args.processed_root, lambda src: merge_tree(src, cfg.paths.archived / "processed", move=move)),
    ):
        if not source_arg:
            continue
        source = Path(source_arg)
        if not source.is_absolute():
            source = (cfg.base_dir / source).resolve()
        if not source.is_dir():
            print(f"FAILED: {label} root is not a directory: {source}", file=sys.stderr)
            return 1
        if label == "raw":
            raw_validation_failures = _validate_raw_export_tree(source)
            for failed_path, reason in raw_validation_failures:
                print(f"RAW SCHEMA VALIDATION FAILED: {failed_path}", file=sys.stderr)
                print(f"  {reason}", file=sys.stderr)
            if raw_validation_failures:
                invalid_paths = {path.resolve() for path, _reason in raw_validation_failures}
                count = _copy_raw_exports_except(source, cfg.paths.archived / "raw", invalid_paths)
            else:
                count = merge(source)
        else:
            count = merge(source)
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
