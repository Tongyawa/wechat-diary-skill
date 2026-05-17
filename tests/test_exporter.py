from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from wechat_diary_core.config import load_config
from wechat_diary_core.weflow_automation.exporter import export_all_chats, export_moments_for


class FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def click_by_name(self, name: str, retries: int = 3) -> None:
        self.calls.append(("click", name, None))

    def click_if_present(self, name: str, timeout: float = 2) -> bool:
        self.calls.append(("click_if_present", name, str(int(timeout))))
        return True

    def click_after_anchor(self, anchor: str, target: str, timeout: float = 30) -> None:
        self.calls.append(("click_after_anchor", anchor, target))

    def set_text(self, field_name: str, text: str) -> None:
        self.calls.append(("set_text", field_name, text))

    def wait_for(self, name: str, timeout: float = 60) -> None:
        self.calls.append(("wait_for", name, str(int(timeout))))

    def wait_for_absent(self, name: str, timeout: float = 60) -> None:
        self.calls.append(("wait_for_absent", name, str(int(timeout))))

    def wait_for_enabled(self, name: str, timeout: float = 60) -> None:
        self.calls.append(("wait_for_enabled", name, str(int(timeout))))

    def wait_for_text_sequence(self, first: str, second: str, timeout: float = 60) -> None:
        self.calls.append(("wait_for_text_sequence", f"{first}->{second}", str(int(timeout))))

    def ensure_selected(self, name: str, timeout: float = 60) -> None:
        self.calls.append(("ensure_selected", name, str(int(timeout))))

    def ensure_checked(self, name: str, timeout: float = 60) -> None:
        self.calls.append(("ensure_checked", name, str(int(timeout))))

    def ensure_action_available(self, action_name: str, trigger_name: str, timeout: float = 60) -> None:
        self.calls.append(("ensure_action_available", action_name, trigger_name))

    def close_any_modal(self, timeout: float = 5) -> int:
        self.calls.append(("close_any_modal", "", str(int(timeout))))
        return 0

    def close_current_modal(self, timeout: float = 5) -> bool:
        self.calls.append(("close_current_modal", "", str(int(timeout))))
        return False

    def snapshot_task_rows(self) -> list:
        self.calls.append(("snapshot_task_rows", "", None))
        return []

    def wait_for_new_task_completion(
        self,
        baseline,
        title_contains: str,
        status: str = "已完成",
        timeout: float = 1800,
        poll_interval: float = 1.0,
    ):
        self.calls.append(("wait_for_new_task_completion", title_contains, str(int(timeout))))
        from wechat_diary_core.weflow_automation.cdp_driver import TaskRow

        return TaskRow(title=title_contains, status=status, signature="fake")

    def screenshot(self) -> bytes:
        return b""


def test_config() -> tuple[Any, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    config_path = root / "config.toml"
    config_path.write_text(
        f"""
[paths]
raw = "{(root / 'raw').as_posix()}"

[automation]
weflow_exe = "{(root / 'WeFlow.exe').as_posix()}"
""".strip(),
        encoding="utf-8",
    )
    return load_config(config_path), tmp


class ExporterTests(unittest.TestCase):
    def test_export_all_chats_runs_expected_sequence(self) -> None:
        cfg, tmp = test_config()
        self.addCleanup(tmp.cleanup)
        driver = FakeDriver()

        result = export_all_chats("2026-05-13", config=cfg, driver=driver, cleanup="skip")  # type: ignore[arg-type]

        self.assertEqual(result.kind, "all_chats")
        self.assertEqual(
            [(call[0], call[1]) for call in driver.calls],
            [
                ("close_any_modal", ""),
                # Pre-flight: snapshot the task center so we can diff later
                ("wait_for_enabled", "任务中心"),
                ("click", "任务中心"),
                ("snapshot_task_rows", ""),
                ("close_current_modal", ""),
                # Trigger the export
                ("click", "导出"),
                ("wait_for", "自动化导出"),
                ("click", "自动化导出"),
                ("wait_for_enabled", "立即执行"),
                ("click", "立即执行"),
                ("close_current_modal", ""),
                # Wait for the new row to reach 已完成
                ("wait_for_enabled", "任务中心"),
                ("click", "任务中心"),
                ("wait_for_new_task_completion", "自动化导出"),
                ("close_current_modal", ""),
                ("click", "首页"),
            ],
        )

    def test_export_moments_for_keeps_target_list_outside_core_logic(self) -> None:
        cfg, tmp = test_config()
        self.addCleanup(tmp.cleanup)
        driver = FakeDriver()

        with patch("wechat_diary_core.weflow_automation.driver.confirm_native_dialog") as confirm:
            result = export_moments_for(["wxid_a", "wxid_b"], "2026-05-13", config=cfg, driver=driver)  # type: ignore[arg-type]

        self.assertEqual(result.kind, "moments")
        self.assertEqual((result.commands[0].kind, result.commands[0].name), ("close_any_modal", ""))
        self.assertEqual((result.commands[1].kind, result.commands[1].name), ("wait_for_absent", "导出格式"))
        self.assertEqual((result.commands[2].kind, result.commands[2].name), ("wait_for", "朋友圈"))
        self.assertEqual((result.commands[3].kind, result.commands[3].name, result.commands[3].value), ("click_after_anchor", "聊天", "朋友圈"))
        self.assertEqual((result.commands[4].kind, result.commands[4].name), ("wait_for", "查找联系人"))
        self.assertEqual((result.commands[5].kind, result.commands[5].name), ("click", "查找联系人"))
        self.assertIn(("set_text", "查找联系人", "wxid_a"), driver.calls)
        self.assertIn(("set_text", "查找联系人", "wxid_b"), driver.calls)
        self.assertGreaterEqual(driver.calls.count(("wait_for", "条", "30")), 2)
        self.assertGreaterEqual(driver.calls.count(("click_after_anchor", "全选", "选择")), 2)
        self.assertIn(("ensure_action_available", "下载所选", "全选"), driver.calls)
        self.assertEqual(
            [(command.kind, command.name) for command in result.commands[-29:]],
            [
                ("set_text", "查找联系人"),
                ("wait_for", "条"),
                ("click_after_anchor", "全选"),
                ("ensure_action_available", "下载所选"),
                ("click", "下载所选"),
                ("wait_for", "导出格式"),
                ("wait_for", "联系人"),
                ("wait_for", "联系人"),
                ("click", "JSON"),
                ("click", "点击选择输出目录"),
                ("confirm_native_dialog", "选择导出目录"),
                ("click", "全部时间"),
                ("wait_for", "时间范围设置"),
                ("click", "昨天"),
                ("wait_for_enabled", "确认"),
                ("click", "确认"),
                ("wait_for_absent", "时间范围设置"),
                ("wait_for", "昨天"),
                ("ensure_checked", "图片"),
                ("ensure_checked", "实况图"),
                ("ensure_checked", "视频"),
                ("wait_for", "联系人"),
                ("wait_for", "联系人"),
                ("wait_for_enabled", "开始导出"),
                ("click", "开始导出"),
                ("wait_for", "完成"),
                ("click", "完成"),
                ("close_current_modal", ""),
                ("click", "首页"),
            ],
        )
        confirm.assert_called_once_with(title="选择导出目录", confirm_name="选择文件夹", timeout=30)


if __name__ == "__main__":
    unittest.main()
