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

    def set_text(self, field_name: str, text: str) -> None:
        self.calls.append(("set_text", field_name, text))

    def wait_for(self, name: str, timeout: float = 60) -> None:
        self.calls.append(("wait_for", name, str(int(timeout))))

    def wait_for_enabled(self, name: str, timeout: float = 60) -> None:
        self.calls.append(("wait_for_enabled", name, str(int(timeout))))

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

        result = export_all_chats("2026-05-13", config=cfg, driver=driver)  # type: ignore[arg-type]

        self.assertEqual(result.kind, "all_chats")
        self.assertEqual(
            [(call[0], call[1]) for call in driver.calls],
            [
                ("click", "导出"),
                ("wait_for", "自动化导出"),
                ("click", "自动化导出"),
                ("wait_for_enabled", "立即执行"),
                ("click", "立即执行"),
                ("wait_for", "任务中心"),
                ("click", "任务中心"),
                ("wait_for", "已完成"),
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
        self.assertIn(("set_text", "查找联系人", "wxid_a"), driver.calls)
        self.assertIn(("set_text", "查找联系人", "wxid_b"), driver.calls)
        self.assertEqual(
            [(command.kind, command.name) for command in result.commands[-12:]],
            [
                ("click", "选择 wxid_b"),
                ("wait_for_enabled", "导出朋友圈"),
                ("click", "导出朋友圈"),
                ("wait_for", "导出格式"),
                ("click", "JSON"),
                ("click", "点击选择输出目录"),
                ("confirm_native_dialog", "选择导出目录"),
                ("wait_for", "昨天"),
                ("wait_for_enabled", "开始导出"),
                ("click", "开始导出"),
                ("wait_for", "已完成"),
                ("click", "首页"),
            ],
        )
        confirm.assert_called_once_with(title="选择导出目录", confirm_name="选择文件夹", timeout=30)


if __name__ == "__main__":
    unittest.main()
