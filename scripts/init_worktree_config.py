from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_daily_export import _set_toml_value, _toml_string  # noqa: E402


PATH_KEYS = ("raw", "processed", "archived", "insights")
DEFAULT_PATHS = {
    "raw": "WeFlow-raw-exports",
    "processed": "WeFlow-processed-exports",
    "archived": "WeFlow-archived-exports",
    "insights": "WeFlow-insights",
}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description=(
            "Initialize a gitignored config.toml inside a git worktree so the "
            "real-machine daily export can run there: the worktree supplies the "
            "code under test, while every data root points (absolute) at the "
            "main workspace — which is also where WeFlow's global in-app export "
            "directory already writes."
        )
    )
    parser.add_argument(
        "--main-root",
        default="",
        help=(
            "Main data-workspace root. Defaults to the nearest ancestor with "
            "config.toml, then the first entry of `git worktree list`."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing config.toml in this worktree.",
    )
    args = parser.parse_args(argv)

    try:
        worktree_root = _detect_current_worktree_root()
        main_root = (
            Path(args.main_root).resolve()
            if args.main_root
            else _detect_main_root(worktree_root)
        )
        target = init_worktree_config(worktree_root, main_root, force=args.force)
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"Worktree config written: {target}")
    print(f"Data roots point at the main workspace: {main_root}")
    print("注意：在本 worktree 实测 = 用 worktree 的代码操作主工作区的真实数据；同一时间只在一个工作区跑导出。")
    return 0


def _detect_current_worktree_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    return Path(result.stdout.strip()).resolve()


def _detect_main_root(worktree_root: Path) -> Path:
    # In the three-repository layout, a public-skill worktree lives under the
    # private data workspace. Its Git main tree is the public skill repository,
    # not the workspace whose config owns the real data roots.
    for ancestor in worktree_root.parents:
        if (ancestor / "config.toml").is_file():
            return ancestor.resolve()

    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            # The first entry is always the main working tree.
            return Path(line[len("worktree "):]).resolve()
    raise RuntimeError("Could not detect the main workspace via `git worktree list`.")


def init_worktree_config(worktree_root: Path, main_root: Path, *, force: bool = False) -> Path:
    worktree_root = Path(worktree_root).resolve()
    main_root = Path(main_root).resolve()
    if worktree_root == main_root:
        raise RuntimeError("Already in the main workspace; nothing to initialize.")

    main_config = main_root / "config.toml"
    if not main_config.exists():
        raise FileNotFoundError(f"Main workspace has no config.toml: {main_config}")

    target = worktree_root / "config.toml"
    if target.exists() and not force:
        raise RuntimeError(f"{target} already exists; pass --force to overwrite.")

    text = main_config.read_text(encoding="utf-8")
    data = tomllib.loads(text)

    paths_section = data.get("paths") or {}
    for key in PATH_KEYS:
        value = str(paths_section.get(key) or DEFAULT_PATHS[key])
        absolute = _anchor(value, main_root)
        text = _set_toml_value(text, "paths", key, _toml_string(absolute))

    # An optional local voice fallback script lives only in the main workspace
    # (its directory is gitignored, so a worktree never carries it).
    fallback = str((data.get("daily_export") or {}).get("voice_fallback_script") or "").strip()
    if fallback:
        text = _set_toml_value(
            text, "daily_export", "voice_fallback_script", _toml_string(_anchor(fallback, main_root))
        )

    target.write_text(text, encoding="utf-8")
    return target


def _anchor(value: str, main_root: Path) -> str:
    path = Path(value).expanduser()
    resolved = path if path.is_absolute() else (main_root / path)
    return resolved.resolve().as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
