"""Legacy WeFlow <=4.x GUI backend."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, ClassVar

from ...config import Config
from .cdp_driver import CdpDriver
from .driver import DriverUnavailable, ElementNotFound
from .exporter import export_all_chats, export_moments_for, wait_for_export_tasks_idle
from .launcher import (
    WeFlowSession,
    assert_single_weflow_instance,
    ensure_weflow_running,
    stop_weflow_processes,
)
from .voice_transcribe import batch_transcribe_voices_for


@dataclass
class WeflowBackend:
    """Legacy GUI/CDP adapter retained for WeFlow 4.x and earlier."""

    name: ClassVar[str] = "weflow"
    capabilities: ClassVar[frozenset[str]] = frozenset({"moments", "voice_transcribe"})

    config: Config
    stop_processes: Callable[..., Any] = field(default=stop_weflow_processes, repr=False)
    ensure_running: Callable[..., Any] = field(default=ensure_weflow_running, repr=False)
    wait_ready: Callable[..., Any] = field(default=None, repr=False)  # type: ignore[assignment]
    assert_single_instance: Callable[..., Any] = field(default=assert_single_weflow_instance, repr=False)
    export_all: Callable[..., Any] = field(default=export_all_chats, repr=False)
    export_moments_for: Callable[..., Any] = field(default=export_moments_for, repr=False)
    wait_tasks_idle: Callable[..., Any] = field(default=wait_for_export_tasks_idle, repr=False)
    transcribe_for: Callable[..., Any] = field(default=batch_transcribe_voices_for, repr=False)
    _session: Any = field(default=None, init=False, repr=False)
    _moments_ready: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.wait_ready is None:
            self.wait_ready = wait_for_ready_page

    def prepare(self) -> None:
        """Stop, launch and wait for the legacy GUI automation surface."""

        if self.config.daily_export.restart_weflow:
            stopped = self.stop_processes(timeout=self.config.automation.launch_timeout_sec)
            if stopped is False:
                raise RuntimeError("Timed out waiting for existing WeFlow processes to exit.")
        self._session = self.ensure_running(self.config)
        endpoint = getattr(self._session, "cdp_endpoint", None)
        if endpoint:
            self.wait_ready(endpoint)
        self._guard("prepare")

    def transcribe_voices(self, usernames: list[str]) -> None:
        self._guard("transcribe_voices:before")
        self.transcribe_for(usernames, config=self.config)
        self._guard("transcribe_voices:after")

    def export_chats(self, export_date: date) -> None:
        self._guard("export_chats:before")
        self.export_all(date=export_date, config=self.config, cleanup="skip")
        self._guard("export_chats:after")

    def export_moments(self, usernames: list[str], export_date: date) -> None:
        self._guard("export_moments:before")
        if not self._moments_ready and getattr(self._session, "cdp_endpoint", None):
            self.wait_tasks_idle(config=self.config, title_contains="自动化导出")
            self._moments_ready = True
        self.export_moments_for(usernames, date=export_date, config=self.config)
        self._guard("export_moments:after")

    def shutdown(self) -> None:
        """Keep the user-visible legacy app open, matching the old runner."""

    def _guard(self, stage: str) -> None:
        if not isinstance(self._session, WeFlowSession):
            return
        try:
            self.assert_single_instance(self._session)
        except Exception as exc:
            raise RuntimeError(f"{stage} WeFlow instance guard failed: {exc}") from exc


def wait_for_ready_page(endpoint: str, timeout: float = 60) -> None:
    """Wait until the legacy WeFlow page exposes its automation controls."""

    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        driver: CdpDriver | None = None
        try:
            driver = CdpDriver.connect(endpoint)
            driver.wait_for("朋友圈", timeout=3)
            return
        except (DriverUnavailable, ElementNotFound, OSError) as exc:
            last_error = exc
            time.sleep(1)
        finally:
            if driver is not None:
                driver.close()
    raise DriverUnavailable(f"WeFlow page did not become ready after launch: {last_error}")


__all__ = ["WeflowBackend", "wait_for_ready_page"]
