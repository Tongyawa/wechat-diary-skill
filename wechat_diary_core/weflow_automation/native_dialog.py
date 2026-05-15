from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
import ctypes
import time


BM_CLICK = 0x00F5
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9
VK_RETURN = 0x0D


class NativeDialogError(RuntimeError):
    """Base error raised while handling a native Windows dialog."""


class NativeDialogTimeout(NativeDialogError):
    """Raised when a native dialog does not appear before timeout."""


class NativeDialogFocusError(NativeDialogError):
    """Raised when fallback Enter would target the wrong foreground window."""


class NativeDialogCloseTimeout(NativeDialogError):
    """Raised when a native dialog accepted input but did not close."""


class NativeWindowController(Protocol):
    def find_visible_window(self, title_contains: str) -> int | None: ...

    def find_child_button(self, parent_hwnd: int, name_contains: str) -> int | None: ...

    def click_button(self, button_hwnd: int) -> None: ...

    def focus_window(self, hwnd: int) -> None: ...

    def foreground_title(self) -> str: ...

    def send_enter(self) -> None: ...


ConfirmMethod = Literal["button", "enter"]


@dataclass(frozen=True)
class NativeDialogResult:
    hwnd: int
    method: ConfirmMethod
    button_hwnd: int | None = None


def confirm_native_dialog(
    title: str,
    confirm_name: str = "选择文件夹",
    timeout: float = 30,
    interval: float = 0.25,
    foreground_timeout: float = 1.0,
    close_timeout: float = 3.0,
    controller: NativeWindowController | None = None,
) -> NativeDialogResult:
    active_controller = controller or Win32WindowController()
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        hwnd = active_controller.find_visible_window(title)
        if hwnd is None:
            time.sleep(interval)
            continue

        button_hwnd = active_controller.find_child_button(hwnd, confirm_name)
        if button_hwnd is not None:
            active_controller.click_button(button_hwnd)
            if _wait_for_window_closed(active_controller, title, timeout=close_timeout):
                return NativeDialogResult(hwnd=hwnd, method="button", button_hwnd=button_hwnd)
            _confirm_with_enter(active_controller, hwnd, title, foreground_timeout)
            if _wait_for_window_closed(active_controller, title, timeout=close_timeout):
                return NativeDialogResult(hwnd=hwnd, method="enter", button_hwnd=button_hwnd)
            raise NativeDialogCloseTimeout(f"Native dialog {title!r} did not close after button click and Enter fallback.")

        if _confirm_with_enter(active_controller, hwnd, title, foreground_timeout):
            if _wait_for_window_closed(active_controller, title, timeout=close_timeout):
                return NativeDialogResult(hwnd=hwnd, method="enter")
            raise NativeDialogCloseTimeout(f"Native dialog {title!r} did not close after Enter.")

        foreground = active_controller.foreground_title()
        raise NativeDialogFocusError(
            f"Refused to send Enter. Expected foreground title containing {title!r}, got {foreground!r}."
        )

    raise NativeDialogTimeout(f"Timed out waiting for native dialog title containing {title!r}.")


def _confirm_with_enter(
    controller: NativeWindowController,
    hwnd: int,
    title: str,
    foreground_timeout: float,
) -> bool:
    controller.focus_window(hwnd)
    if _wait_for_foreground_title(controller, title, timeout=foreground_timeout):
        controller.send_enter()
        return True
    return False


def _wait_for_foreground_title(controller: NativeWindowController, title: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if title in controller.foreground_title():
            return True
        time.sleep(0.05)
    return False


def _wait_for_window_closed(controller: NativeWindowController, title: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if controller.find_visible_window(title) is None:
            return True
        time.sleep(0.05)
    return False


class Win32WindowController:
    def __init__(self) -> None:
        if not hasattr(ctypes, "windll"):
            raise NativeDialogError("Native dialog handling is only available on Windows.")
        self.user32 = ctypes.windll.user32

    def find_visible_window(self, title_contains: str) -> int | None:
        matches: list[int] = []
        enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd: int, _lparam: int) -> bool:
            if not self.user32.IsWindowVisible(hwnd):
                return True
            title = self._window_text(hwnd)
            if title_contains in title:
                matches.append(hwnd)
                return False
            return True

        self.user32.EnumWindows(enum_proc_type(callback), 0)
        return matches[0] if matches else None

    def find_child_button(self, parent_hwnd: int, name_contains: str) -> int | None:
        matches: list[int] = []
        enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd: int, _lparam: int) -> bool:
            text = self._window_text(hwnd)
            if name_contains in text:
                matches.append(hwnd)
                return False
            return True

        self.user32.EnumChildWindows(parent_hwnd, enum_proc_type(callback), 0)
        return matches[0] if matches else None

    def click_button(self, button_hwnd: int) -> None:
        self.user32.SendMessageW(button_hwnd, BM_CLICK, 0, 0)

    def focus_window(self, hwnd: int) -> None:
        self.user32.ShowWindow(hwnd, SW_RESTORE)
        self.user32.BringWindowToTop(hwnd)
        self.user32.SetForegroundWindow(hwnd)

    def foreground_title(self) -> str:
        hwnd = self.user32.GetForegroundWindow()
        return self._window_text(hwnd) if hwnd else ""

    def send_enter(self) -> None:
        self.user32.keybd_event(VK_RETURN, 0, 0, 0)
        self.user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)

    def _window_text(self, hwnd: int) -> str:
        length = self.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
