from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .native_dialog import confirm_native_dialog


class DriverError(RuntimeError):
    """Base error raised by GUI automation drivers."""


class DriverUnavailable(DriverError):
    """Raised when a configured driver cannot run in the current environment."""


class ElementNotFound(DriverError):
    """Raised when a driver cannot locate a requested UI element."""


class TaskFailed(DriverError):
    """Raised when a new task-center row reaches a terminal failure state."""


class Driver(Protocol):
    def click_by_name(self, name: str, retries: int = 3) -> None: ...

    def click_if_present(self, name: str, timeout: float = 2) -> bool: ...

    def click_after_anchor(self, anchor: str, target: str, timeout: float = 30) -> None:
        """Click the first interactable element whose visible text contains ``target``
        AND that appears in the DOM after a visible element containing ``anchor``.

        Used to disambiguate the same display name appearing under multiple sections
        (e.g. "聊天记录" vs "联系人" both showing the same wxid).
        """
        ...

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

    def snapshot_task_rows(self) -> list[Any]:
        """Return the visible task-center rows (TaskRow-like objects).

        Used to diff `before / after 立即执行` so we can wait for the *new* task
        to reach 已完成 instead of falsely matching an older 已完成 row.
        """
        ...

    def wait_for_new_task_completion(
        self,
        baseline: set[str],
        title_contains: str,
        status: str = "已完成",
        timeout: float = 1800,
        poll_interval: float = 1.0,
    ) -> Any:
        """Wait until a task row appears whose signature is not in baseline."""
        ...

    def screenshot(self) -> bytes: ...


CommandKind = Literal[
    "click",
    "click_if_present",
    "click_after_anchor",
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
    "capture_task_baseline",
    "wait_for_new_task_completion",
]


@dataclass(frozen=True)
class DriverCommand:
    kind: CommandKind
    name: str = ""
    value: str | None = None
    timeout: float | None = None
    retries: int = 3
    poll_interval: float | None = None


@dataclass
class ExporterContext:
    """Mutable per-run state shared between sequenced driver commands."""

    task_baselines: dict[str, set[str]] = field(default_factory=dict)


def run_driver_command(
    driver: Driver,
    command: DriverCommand,
    context: ExporterContext | None = None,
) -> None:
    if command.kind == "click":
        driver.click_by_name(command.name, retries=command.retries)
        return
    if command.kind == "click_if_present":
        driver.click_if_present(command.name, timeout=command.timeout or 2)
        return
    if command.kind == "click_after_anchor":
        driver.click_after_anchor(command.name, command.value or "", timeout=command.timeout or 30)
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
    if command.kind == "capture_task_baseline":
        if context is None:
            raise DriverError("capture_task_baseline requires an ExporterContext.")
        rows = driver.snapshot_task_rows()
        context.task_baselines[command.name] = {row.signature for row in rows}
        return
    if command.kind == "wait_for_new_task_completion":
        if context is None:
            raise DriverError("wait_for_new_task_completion requires an ExporterContext.")
        baseline = context.task_baselines.get(command.name, set())
        driver.wait_for_new_task_completion(
            baseline=baseline,
            title_contains=command.value or "",
            timeout=command.timeout or 1800,
            poll_interval=command.poll_interval or 1.0,
        )
        return
    raise DriverError(f"Unsupported driver command: {command.kind}")
