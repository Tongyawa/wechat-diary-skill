from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_diary_core.config import load_config
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
        config_path = ROOT / config_path
    cfg = load_config(config_path)
    move = not args.keep_source

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
        count = merge(source)
        print(f"Merged {count} {label} files into {cfg.paths.archived / label}")
        if move:
            # Moving leaves empty directory shells behind; drop the whole source.
            shutil.rmtree(source, ignore_errors=True)
            print(f"Removed emptied source: {source}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
