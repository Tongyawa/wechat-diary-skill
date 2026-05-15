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
            return NativeDialogResult(hwnd=hwnd, method="button", button_hwnd=button_hwnd)

        active_controller.focus_window(hwnd)
        if _wait_for_foreground_title(active_controller, title, timeout=foreground_timeout):
            active_controller.send_enter()
            return NativeDialogResult(hwnd=hwnd, method="enter")

        foreground = active_controller.foreground_title()
        raise NativeDialogFocusError(
            f"Refused to send Enter. Expected foreground title containing {title!r}, got {foreground!r}."
        )

    raise NativeDialogTimeout(f"Timed out waiting for native dialog title containing {title!r}.")


def _wait_for_foreground_title(controller: NativeWindowController, title: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if title in controller.foreground_title():
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
