from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_diary_core.config import Config, load_config
from wechat_diary_core.weflow_automation.cdp_driver import CdpDriver, fetch_cdp_targets, select_page_target
from wechat_diary_core.weflow_automation.exporter import export_all_chats, export_moments_for
from wechat_diary_core.weflow_automation.launcher import ensure_weflow_running
from wechat_diary_core.weflow_automation.native_dialog import Win32WindowController, confirm_native_dialog


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Validate WeFlow CDP automation and local export acceptance flows.")
    parser.add_argument("--config", default="config.toml", help="Path to config.toml.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("snapshot", help="Print CDP targets and visible UI elements.")

    picker = subparsers.add_parser("moments-picker", help="Validate Moments folder picker and cancel before export.")
    picker.add_argument("--contact", required=True, help="Display text to type into the Moments contact filter.")

    subparsers.add_parser("all-chats-export", help="Run the all-chats export flow and report output changes.")

    moments_export = subparsers.add_parser("moments-export", help="Run the Moments export flow and report output changes.")
    moments_export.add_argument("--contact", required=True, help="Display text to type into the Moments contact filter.")

    args = parser.parse_args()
    cfg = load_config(args.config)

    if args.command == "all-chats-export":
        return _export_acceptance(cfg, "all chats", lambda: export_all_chats(config=cfg))
    if args.command == "moments-export":
        return _export_acceptance(cfg, "moments", lambda: export_moments_for([args.contact], config=cfg))

    session = ensure_weflow_running(cfg)
    if not session.cdp_endpoint:
        raise RuntimeError("CDP endpoint is unavailable.")

    if args.command == "snapshot":
        return _snapshot(session.cdp_endpoint)
    if args.command == "moments-picker":
        return _moments_picker(session.cdp_endpoint, args.contact)
    return 1


def _snapshot(endpoint: str) -> int:
    targets = fetch_cdp_targets(endpoint)
    print("CDP targets:")
    for target in targets:
        print(f"- {target.get('type')} | {target.get('title')} | {target.get('url')}")
    print(f"Selected target: {select_page_target(targets)}")

    driver = CdpDriver.connect(endpoint)
    try:
        print("Visible elements:")
        for index, element in enumerate(driver.visible_elements(200), 1):
            print(f"{index:03d} {element}")
    finally:
        driver.close()
    return 0


def _moments_picker(endpoint: str, contact: str) -> int:
    driver = CdpDriver.connect(endpoint)
    try:
        driver.click_by_name("朋友圈")
        driver.wait_for("查找联系人", timeout=10)
        driver.set_text("查找联系人", contact)
        driver.wait_for_enabled(f"选择 {contact}", timeout=10)
        driver.click_by_name(f"选择 {contact}")
        driver.wait_for_enabled("导出朋友圈", timeout=10)
        driver.click_by_name("导出朋友圈")
        driver.wait_for("导出格式", timeout=10)
        driver.click_by_name("JSON")
        driver.click_by_name("点击选择输出目录")

        result = confirm_native_dialog("选择导出目录", "选择文件夹", timeout=15, close_timeout=5)
        still_open = Win32WindowController().find_visible_window("选择导出目录") is not None
        driver.wait_for_enabled("开始导出", timeout=10)
        driver.click_by_name("取消")
        driver.set_text("查找联系人", "")
    finally:
        driver.close()

    print(f"Native dialog result: {result}")
    print(f"Native dialog still open: {still_open}")
    print("Start export became enabled: True")
    print("Manual checkpoint: during total Step 7-9 acceptance, watch whether multiple folder picker windows open briefly.")
    return 1 if still_open else 0


def _export_acceptance(cfg: Config, label: str, run_export: Callable[[], object]) -> int:
    output_root = cfg.paths.raw
    before = _output_snapshot(output_root)
    started_at = time.time()

    run_export()

    changed = _changed_output_entries(output_root, before, started_at)
    print(f"Export flow completed: {label}")
    print(f"Output root: {output_root}")
    if changed:
        print(f"New or touched top-level output entries: {len(changed)}")
        print("Inspect the output root manually if exact entry names are needed.")
        return 0

    print("No new or touched top-level output entry was detected; inspect WeFlow and the output directory manually.")
    return 2


def _output_snapshot(root: Path) -> dict[str, float]:
    if not root.exists():
        return {}
    return {child.name: _latest_modified_at(child) for child in root.iterdir()}


def _changed_output_entries(root: Path, before: dict[str, float], started_at: float) -> list[str]:
    if not root.exists():
        return []
    changed: list[str] = []
    for child in root.iterdir():
        modified_at = _latest_modified_at(child)
        if child.name not in before or modified_at > before[child.name] or modified_at >= started_at - 1:
            changed.append(child.name)
    return sorted(changed)


def _latest_modified_at(path: Path) -> float:
    try:
        latest = path.stat().st_mtime
    except OSError:
        return 0
    if not path.is_dir():
        return latest
    for child in path.rglob("*"):
        try:
            latest = max(latest, child.stat().st_mtime)
        except OSError:
            continue
    return latest


if __name__ == "__main__":
    raise SystemExit(main())
