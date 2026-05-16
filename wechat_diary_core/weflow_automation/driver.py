from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .native_dialog import confirm_native_dialog


class DriverError(RuntimeError):
    """Base error raised by GUI automation drivers."""


class DriverUnavailable(DriverError):
    """Raised when a configured driver cannot run in the current environment."""


class ElementNotFound(DriverError):
    """Raised when a driver cannot locate a requested UI element."""


class Driver(Protocol):
    def click_by_name(self, name: str, retries: int = 3) -> None: ...

    def click_if_present(self, name: str, timeout: float = 2) -> bool: ...

    def set_text(self, field_name: str, text: str) -> None: ...

    def wait_for(self, name: str, timeout: float = 60) -> None: ...

    def wait_for_absent(self, name: str, timeout: float = 60) -> None: ...

    def wait_for_enabled(self, name: str, timeout: float = 60) -> None: ...

    def wait_for_text_sequence(self, first: str, second: str, timeout: float = 60) -> None: ...

    def ensure_selected(self, name: str, timeout: float = 60) -> None: ...

    def ensure_checked(self, name: str, timeout: float = 60) -> None: ...

    def ensure_action_available(self, action_name: str, trigger_name: str, timeout: float = 60) -> None: ...

    def close_any_modal(self, timeout: float = 5) -> int:
        """Close every dismissable modal currently on screen.

        Driver implementation chooses the strategy (X / cancel / Esc / overlay
        click); spec only requires that all visible dismissable modals are
        closed. Returns the number of modals dismissed.
        """
        ...

    def close_current_modal(self, timeout: float = 5) -> bool:
        """Close just the topmost modal. Driver picks the dismissal strategy.

        Returns True if a modal was dismissed, False if none was found.
        """
        ...

    def screenshot(self) -> bytes: ...


CommandKind = Literal[
    "click",
    "click_if_present",
    "set_text",
    "wait_for",
    "wait_for_absent",
    "wait_for_enabled",
    "wait_for_text_sequence",
    "ensure_selected",
    "ensure_checked",
    "ensure_action_available",
    "close_any_modal",
    "close_current_modal",
    "confirm_native_dialog",
]


@dataclass(frozen=True)
class DriverCommand:
    kind: CommandKind
    name: str = ""
    value: str | None = None
    timeout: float | None = None
    retries: int = 3


def run_driver_command(driver: Driver, command: DriverCommand) -> None:
    if command.kind == "click":
        driver.click_by_name(command.name, retries=command.retries)
        return
    if command.kind == "click_if_present":
        driver.click_if_present(command.name, timeout=command.timeout or 2)
        return
    if command.kind == "set_text":
        driver.set_text(command.name, command.value or "")
        return
    if command.kind == "wait_for":
        driver.wait_for(command.name, timeout=command.timeout or 60)
        return
    if command.kind == "wait_for_absent":
        driver.wait_for_absent(command.name, timeout=command.timeout or 60)
        return
    if command.kind == "wait_for_enabled":
        driver.wait_for_enabled(command.name, timeout=command.timeout or 60)
        return
    if command.kind == "wait_for_text_sequence":
        driver.wait_for_text_sequence(command.name, command.value or "", timeout=command.timeout or 60)
        return
    if command.kind == "ensure_selected":
        driver.ensure_selected(command.name, timeout=command.timeout or 60)
        return
    if command.kind == "ensure_checked":
        driver.ensure_checked(command.name, timeout=command.timeout or 60)
        return
    if command.kind == "ensure_action_available":
        driver.ensure_action_available(command.name, command.value or "", timeout=command.timeout or 60)
        return
    if command.kind == "close_any_modal":
        driver.close_any_modal(timeout=command.timeout or 5)
        return
    if command.kind == "close_current_modal":
        driver.close_current_modal(timeout=command.timeout or 5)
        return
    if command.kind == "confirm_native_dialog":
        confirm_native_dialog(
            title=command.name,
            confirm_name=command.value or "选择文件夹",
            timeout=command.timeout or 30,
        )
        return
    raise DriverError(f"Unsupported driver command: {command.kind}")
