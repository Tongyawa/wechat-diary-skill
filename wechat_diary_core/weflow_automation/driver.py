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

    def set_text(self, field_name: str, text: str) -> None: ...

    def wait_for(self, name: str, timeout: float = 60) -> None: ...

    def screenshot(self) -> bytes: ...


CommandKind = Literal["click", "set_text", "wait_for", "confirm_native_dialog"]


@dataclass(frozen=True)
class DriverCommand:
    kind: CommandKind
    name: str
    value: str | None = None
    timeout: float | None = None
    retries: int = 3


def run_driver_command(driver: Driver, command: DriverCommand) -> None:
    if command.kind == "click":
        driver.click_by_name(command.name, retries=command.retries)
        return
    if command.kind == "set_text":
        driver.set_text(command.name, command.value or "")
        return
    if command.kind == "wait_for":
        driver.wait_for(command.name, timeout=command.timeout or 60)
        return
    if command.kind == "confirm_native_dialog":
        confirm_native_dialog(
            title=command.name,
            confirm_name=command.value or "选择文件夹",
            timeout=command.timeout or 30,
        )
        return
    raise DriverError(f"Unsupported driver command: {command.kind}")
