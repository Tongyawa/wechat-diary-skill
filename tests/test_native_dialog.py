from __future__ import annotations

import unittest
from unittest.mock import patch

from wechat_diary_core.weflow_automation.driver import DriverCommand, run_driver_command
from wechat_diary_core.weflow_automation.native_dialog import (
    NativeDialogCloseTimeout,
    NativeDialogFocusError,
    NativeDialogTimeout,
    confirm_native_dialog,
)


class FakeController:
    def __init__(
        self,
        window: int | None,
        button: int | None,
        foreground: str = "",
        close_after_click: bool = True,
        close_after_enter: bool = True,
    ) -> None:
        self.windows = [window]
        self.button = button
        self.foreground = foreground
        self.close_after_click = close_after_click
        self.close_after_enter = close_after_enter
        self.actions: list[str] = []

    def find_visible_window(self, title_contains: str) -> int | None:
        self.actions.append(f"find_window:{title_contains}")
        return self.windows[-1]

    def find_child_button(self, parent_hwnd: int, name_contains: str) -> int | None:
        self.actions.append(f"find_button:{parent_hwnd}:{name_contains}")
        return self.button

    def click_button(self, button_hwnd: int) -> None:
        self.actions.append(f"click:{button_hwnd}")
        if self.close_after_click:
            self.windows.append(None)

    def focus_window(self, hwnd: int) -> None:
        self.actions.append(f"focus:{hwnd}")

    def foreground_title(self) -> str:
        self.actions.append("foreground")
        return self.foreground

    def send_enter(self) -> None:
        self.actions.append("enter")
        if self.close_after_enter:
            self.windows.append(None)


class FakeDriver:
    def click_by_name(self, name: str, retries: int = 3) -> None:
        raise AssertionError("not used")

    def set_text(self, field_name: str, text: str) -> None:
        raise AssertionError("not used")

    def wait_for(self, name: str, timeout: float = 60) -> None:
        raise AssertionError("not used")

    def wait_for_enabled(self, name: str, timeout: float = 60) -> None:
        raise AssertionError("not used")

    def screenshot(self) -> bytes:
        return b""


class NativeDialogTests(unittest.TestCase):
    def test_confirm_native_dialog_prefers_button_click(self) -> None:
        controller = FakeController(window=100, button=200, foreground="wrong window")

        result = confirm_native_dialog("选择导出目录", "选择文件夹", controller=controller)

        self.assertEqual(result.method, "button")
        self.assertEqual(result.button_hwnd, 200)
        self.assertIn("click:200", controller.actions)
        self.assertNotIn("enter", controller.actions)

    def test_confirm_native_dialog_falls_back_to_enter_when_button_does_not_close(self) -> None:
        controller = FakeController(
            window=100,
            button=200,
            foreground="选择导出目录",
            close_after_click=False,
            close_after_enter=True,
        )

        result = confirm_native_dialog(
            "选择导出目录",
            "选择文件夹",
            controller=controller,
            close_timeout=0.01,
            foreground_timeout=0.01,
        )

        self.assertEqual(result.method, "enter")
        self.assertEqual(result.button_hwnd, 200)
        self.assertIn("click:200", controller.actions)
        self.assertIn("enter", controller.actions)

    def test_confirm_native_dialog_errors_when_dialog_never_closes(self) -> None:
        controller = FakeController(
            window=100,
            button=200,
            foreground="选择导出目录",
            close_after_click=False,
            close_after_enter=False,
        )

        with self.assertRaises(NativeDialogCloseTimeout):
            confirm_native_dialog(
                "选择导出目录",
                "选择文件夹",
                controller=controller,
                close_timeout=0.01,
                foreground_timeout=0.01,
            )

        self.assertIn("click:200", controller.actions)
        self.assertIn("enter", controller.actions)

    def test_confirm_native_dialog_sends_enter_only_after_foreground_match(self) -> None:
        controller = FakeController(window=100, button=None, foreground="选择导出目录")

        result = confirm_native_dialog("选择导出目录", "选择文件夹", controller=controller, foreground_timeout=0.01)

        self.assertEqual(result.method, "enter")
        self.assertIn("focus:100", controller.actions)
        self.assertIn("enter", controller.actions)

    def test_confirm_native_dialog_rejects_enter_when_foreground_mismatches(self) -> None:
        controller = FakeController(window=100, button=None, foreground="其他窗口")

        with self.assertRaises(NativeDialogFocusError):
            confirm_native_dialog("选择导出目录", "选择文件夹", controller=controller, foreground_timeout=0.01)

        self.assertIn("focus:100", controller.actions)
        self.assertNotIn("enter", controller.actions)

    def test_confirm_native_dialog_times_out_without_window(self) -> None:
        controller = FakeController(window=None, button=None)

        with self.assertRaises(NativeDialogTimeout):
            confirm_native_dialog("选择导出目录", controller=controller, timeout=0.01, interval=0.001)

        self.assertNotIn("enter", controller.actions)

    def test_driver_command_dispatches_native_dialog_confirmation(self) -> None:
        with patch("wechat_diary_core.weflow_automation.driver.confirm_native_dialog") as confirm:
            run_driver_command(
                FakeDriver(),
                DriverCommand("confirm_native_dialog", "选择导出目录", value="选择文件夹", timeout=12),
            )

        confirm.assert_called_once_with(title="选择导出目录", confirm_name="选择文件夹", timeout=12)


if __name__ == "__main__":
    unittest.main()
