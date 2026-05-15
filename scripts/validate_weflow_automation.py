from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_diary_core.config import load_config
from wechat_diary_core.weflow_automation.cdp_driver import CdpDriver, fetch_cdp_targets, select_page_target
from wechat_diary_core.weflow_automation.launcher import ensure_weflow_running
from wechat_diary_core.weflow_automation.native_dialog import Win32WindowController, confirm_native_dialog


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate WeFlow CDP automation without running exports.")
    parser.add_argument("--config", default="config.toml", help="Path to config.toml.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("snapshot", help="Print CDP targets and visible UI elements.")

    picker = subparsers.add_parser("moments-picker", help="Validate Moments folder picker and cancel before export.")
    picker.add_argument("--contact", required=True, help="Display text to type into the Moments contact filter.")

    args = parser.parse_args()
    cfg = load_config(args.config)
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


if __name__ == "__main__":
    raise SystemExit(main())
