from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
import ctypes
import subprocess
import time
import urllib.error
import urllib.request

from ..config import AutomationConfig, Config, WindowGeometry, load_config


WEFLOW_PROCESS_NAME = "WeFlow.exe"


class WeFlowLaunchTimeout(TimeoutError):
    """Raised when WeFlow does not expose the expected automation endpoint in time."""


class WeFlowExecutableNotFound(FileNotFoundError):
    """Raised when the configured WeFlow executable path does not exist."""


class WeFlowInstanceConflict(RuntimeError):
    """Raised when automation detects more than one visible WeFlow instance."""


@dataclass(frozen=True)
class WeFlowSession:
    driver: str
    cdp_endpoint: str | None
    process_started: bool
    process_id: int | None
    window_normalized: bool
    window_process_ids: tuple[int, ...] = ()


def ensure_weflow_running(config: Config | None = None) -> WeFlowSession:
    cfg = config or load_config()
    automation = cfg.automation
    endpoint = cdp_endpoint_url(automation.electron_cdp_port)

    if automation.driver == "cdp" and is_cdp_available(endpoint):
        normalized = normalize_weflow_window(automation.window_geometry)
        window_pids = tuple(sorted(find_weflow_window_process_ids()))
        _raise_if_multiple_windows(window_pids)
        return WeFlowSession(
            "cdp",
            endpoint,
            process_started=False,
            process_id=None,
            window_normalized=normalized,
            window_process_ids=window_pids,
        )

    process_started = False
    process_id: int | None = None
    process_running = is_weflow_process_running()
    if automation.driver == "cdp" and process_running:
        raise WeFlowLaunchTimeout(
            f"WeFlow is already running, but CDP is unavailable at {endpoint}. "
            "Close WeFlow completely and retry so automation does not start a second instance."
        )
    if automation.driver == "cdp" or not process_running:
        process = launch_weflow(automation, log_path=weflow_log_path(cfg))
        process_started = True
        process_id = process.pid

    if automation.driver == "cdp":
        if not wait_for_cdp(endpoint, timeout=automation.launch_timeout_sec):
            raise WeFlowLaunchTimeout(
                f"WeFlow did not expose CDP at {endpoint}. If WeFlow was already open without the CDP flag, close it and retry."
            )
    else:
        if not wait_for_weflow_process(timeout=automation.launch_timeout_sec):
            raise WeFlowLaunchTimeout("WeFlow process did not appear before timeout.")

    normalized = normalize_weflow_window(automation.window_geometry)
    window_pids = tuple(sorted(find_weflow_window_process_ids()))
    _raise_if_multiple_windows(window_pids)
    return WeFlowSession(
        driver=automation.driver,
        cdp_endpoint=endpoint if automation.driver == "cdp" else None,
        process_started=process_started,
        process_id=process_id,
        window_normalized=normalized,
        window_process_ids=window_pids,
    )


def restart_weflow(config: Config | None = None) -> WeFlowSession:
    cfg = config or load_config()
    if not stop_weflow_processes(timeout=cfg.automation.launch_timeout_sec):
        raise WeFlowLaunchTimeout("Timed out waiting for existing WeFlow processes to exit before restart.")
    return ensure_weflow_running(cfg)


def stop_weflow_processes(timeout: float = 30, interval: float = 0.5) -> bool:
    if is_weflow_process_running():
        subprocess.run(
            ["taskkill", "/IM", WEFLOW_PROCESS_NAME, "/F", "/T"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_weflow_process_running():
            return True
        time.sleep(interval)
    return not is_weflow_process_running()


def launch_weflow(automation: AutomationConfig, *, log_path: Path | None = None) -> subprocess.Popen[Any]:
    if not automation.weflow_exe.exists():
        raise WeFlowExecutableNotFound(str(automation.weflow_exe))

    args = build_launch_args(automation)
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    kwargs: dict[str, Any] = {"close_fds": True}
    if flags:
        kwargs["creationflags"] = flags

    # WeFlow's Electron engine writes verbose logs (per-session export
    # failures, WCDB chatter, updater 404s, stack traces) straight to whatever
    # console it inherits — which bypasses the daily-export pipe filter and
    # floods the user's terminal. Redirect the child's stdio off the console:
    # into a runlog file when we have one, otherwise discard. stdin is always
    # detached so WeFlow can never block on console input.
    kwargs["stdin"] = subprocess.DEVNULL
    log_handle = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_path, "ab")
        kwargs["stdout"] = log_handle
        kwargs["stderr"] = subprocess.STDOUT
    else:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL

    try:
        return subprocess.Popen(args, **kwargs)
    finally:
        # The child inherited its own copy of the handle; drop the parent's so
        # the file isn't held open for the runner's whole lifetime.
        if log_handle is not None:
            log_handle.close()


def weflow_log_path(cfg: Config) -> Path | None:
    """Daily runlog file that absorbs WeFlow's own console output."""
    insights = getattr(getattr(cfg, "paths", None), "insights", None)
    if insights is None:
        return None
    return Path(insights) / ".runlog" / f"weflow-{date.today():%Y-%m-%d}.log"


def build_launch_args(automation: AutomationConfig) -> list[str]:
    args = [str(automation.weflow_exe)]
    if automation.driver == "cdp":
        args.append(f"--remote-debugging-port={automation.electron_cdp_port}")
    elif automation.driver == "uia":
        args.append(automation.electron_accessibility_flag)
    return args


def cdp_endpoint_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def wait_for_cdp(endpoint: str, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_cdp_available(endpoint):
            return True
        time.sleep(interval)
    return False


def is_cdp_available(endpoint: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint}/json/version", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def wait_for_weflow_process(timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_weflow_process_running():
            return True
        time.sleep(interval)
    return False


def is_weflow_process_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {WEFLOW_PROCESS_NAME}", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return WEFLOW_PROCESS_NAME.lower() in result.stdout.lower()


def normalize_weflow_window(geometry: WindowGeometry) -> bool:
    hwnd = _find_weflow_window()
    if not hwnd:
        return False

    width = int(geometry.width)
    height = int(geometry.height)
    if width <= 0 or height <= 0:
        return False

    x = 0 if geometry.x is None else int(geometry.x)
    y = 0 if geometry.y is None else int(geometry.y)
    return bool(ctypes.windll.user32.MoveWindow(hwnd, x, y, width, height, True))


def assert_single_weflow_instance(session: WeFlowSession) -> None:
    """Fail loudly if another visible WeFlow window appears during automation."""
    window_pids = tuple(sorted(find_weflow_window_process_ids()))
    _raise_if_multiple_windows(window_pids)
    baseline = set(getattr(session, "window_process_ids", ()) or ())
    if baseline and window_pids and set(window_pids) != baseline:
        raise WeFlowInstanceConflict(
            "Visible WeFlow window ownership changed during automation "
            f"(baseline={sorted(baseline)}, current={list(window_pids)})."
        )


def find_weflow_window_process_ids() -> set[int]:
    if not hasattr(ctypes, "windll"):
        return set()

    user32 = ctypes.windll.user32
    matches: set[int] = set()

    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title_length = user32.GetWindowTextLengthW(hwnd)
        if title_length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(hwnd, buffer, title_length + 1)
        if "WeFlow" not in buffer.value:
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            matches.add(int(pid.value))
        return True

    user32.EnumWindows(enum_proc_type(callback), 0)
    return matches


def _find_weflow_window() -> int | None:
    if not hasattr(ctypes, "windll"):
        return None

    user32 = ctypes.windll.user32
    matches: list[int] = []

    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title_length = user32.GetWindowTextLengthW(hwnd)
        if title_length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(hwnd, buffer, title_length + 1)
        if "WeFlow" in buffer.value:
            matches.append(hwnd)
            return False
        return True

    user32.EnumWindows(enum_proc_type(callback), 0)
    return matches[0] if matches else None


def _raise_if_multiple_windows(window_pids: tuple[int, ...]) -> None:
    if len(window_pids) > 1:
        raise WeFlowInstanceConflict(f"Multiple visible WeFlow windows detected: pids={list(window_pids)}")
