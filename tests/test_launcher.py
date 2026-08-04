from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_diary_core.config import load_config
from wechat_diary_core.backends.weflow.launcher import (
    WeFlowInstanceConflict,
    WeFlowLaunchTimeout,
    WeFlowSession,
    WeFlowWindow,
    _find_weflow_window,
    assert_single_weflow_instance,
    build_launch_args,
    cdp_endpoint_url,
    ensure_weflow_running,
    find_weflow_windows,
    launch_weflow,
    restart_weflow,
    stop_weflow_processes,
    weflow_log_path,
)


class FakeUser32:
    def __init__(self, windows: list[tuple[int, str, int, bool]]) -> None:
        self.windows = {hwnd: (title, pid, visible) for hwnd, title, pid, visible in windows}

    def EnumWindows(self, callback, lparam) -> bool:
        for hwnd in self.windows:
            if not callback(hwnd, lparam):
                break
        return True

    def IsWindowVisible(self, hwnd: int) -> bool:
        return self.windows[hwnd][2]

    def GetWindowTextLengthW(self, hwnd: int) -> int:
        return len(self.windows[hwnd][0])

    def GetWindowTextW(self, hwnd: int, buffer, _max_count: int) -> int:
        buffer.value = self.windows[hwnd][0]
        return len(buffer.value)

    def GetWindowThreadProcessId(self, hwnd: int, pid_ref) -> int:
        pid_ref._obj.value = self.windows[hwnd][1]
        return 1


class LauncherTests(unittest.TestCase):
    def test_weflow_log_path_is_anchored_to_workspace_not_insights(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            insights = Path(tmp) / "persistent" / "WeFlow-insights"
            workspace.mkdir()
            config_path = workspace / "config.toml"
            config_path.write_text(
                f'''
[paths]
insights = "{insights.as_posix()}"
'''.strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            log_path = weflow_log_path(cfg)

        self.assertIsNotNone(log_path)
        self.assertEqual(log_path.parent, workspace / ".runlog")
        self.assertNotEqual(log_path.parent, insights / ".runlog")

    def test_build_cdp_launch_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "WeFlow.exe"
            exe.write_text("", encoding="utf-8")
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[automation]
driver = "cdp"
weflow_exe = "{exe.as_posix()}"
electron_cdp_port = 9333
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)

        self.assertEqual(build_launch_args(cfg.automation), [str(exe), "--remote-debugging-port=9333"])
        self.assertEqual(cdp_endpoint_url(9333), "http://127.0.0.1:9333")

    def test_build_uia_launch_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "WeFlow.exe"
            exe.write_text("", encoding="utf-8")
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[automation]
driver = "uia"
weflow_exe = "{exe.as_posix()}"
electron_accessibility_flag = "--force-renderer-accessibility"
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)

        self.assertEqual(build_launch_args(cfg.automation), [str(exe), "--force-renderer-accessibility"])

    def test_launch_weflow_redirects_engine_output_into_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "WeFlow.exe"
            exe.write_text("", encoding="utf-8")
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[automation]
driver = "cdp"
weflow_exe = "{exe.as_posix()}"
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            log_path = Path(tmp) / "runlog" / "weflow.log"

            with patch("wechat_diary_core.backends.weflow.launcher.subprocess.Popen") as popen:
                launch_weflow(cfg.automation, log_path=log_path)

            kwargs = popen.call_args.kwargs
            # WeFlow's chatter must never inherit the console.
            self.assertNotIn(kwargs.get("stdout"), (None,))
            self.assertEqual(kwargs.get("stderr"), subprocess.STDOUT)
            self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
            self.assertTrue(log_path.parent.exists())

    def test_launch_weflow_discards_engine_output_without_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "WeFlow.exe"
            exe.write_text("", encoding="utf-8")
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[automation]
driver = "cdp"
weflow_exe = "{exe.as_posix()}"
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            with patch("wechat_diary_core.backends.weflow.launcher.subprocess.Popen") as popen:
                launch_weflow(cfg.automation)

            kwargs = popen.call_args.kwargs
            self.assertEqual(kwargs.get("stdout"), subprocess.DEVNULL)
            self.assertEqual(kwargs.get("stderr"), subprocess.DEVNULL)
            self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)

    def test_stop_weflow_processes_uses_taskkill_when_running(self) -> None:
        with (
            patch("wechat_diary_core.backends.weflow.launcher.is_weflow_process_running", side_effect=[True, False]),
            patch("wechat_diary_core.backends.weflow.launcher.subprocess.run") as run,
        ):
            self.assertTrue(stop_weflow_processes(timeout=0.1, interval=0.01))

        run.assert_called_once()
        self.assertIn("taskkill", run.call_args.args[0])
        self.assertIn("WeFlow.exe", run.call_args.args[0])

    def test_cdp_existing_process_without_cdp_fails_instead_of_launching_second(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "WeFlow.exe"
            exe.write_text("", encoding="utf-8")
            config_path = root / "config.toml"
            config_path.write_text(
                f"""
[automation]
driver = "cdp"
weflow_exe = "{exe.as_posix()}"
electron_cdp_port = 9333
cdp_busy_timeout_sec = 0
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            with (
                patch("wechat_diary_core.backends.weflow.launcher.is_cdp_available", return_value=False),
                patch("wechat_diary_core.backends.weflow.launcher.is_weflow_process_running", return_value=True),
                patch("wechat_diary_core.backends.weflow.launcher.launch_weflow") as launch,
            ):
                with self.assertRaises(WeFlowLaunchTimeout):
                    ensure_weflow_running(cfg)

        launch.assert_not_called()

    def test_cdp_busy_then_recovers_returns_session_without_relaunch(self) -> None:
        # WeFlow's process is alive but CDP is momentarily unresponsive (busy),
        # then recovers within cdp_busy_timeout_sec. Must wait it out and reuse
        # the running instance instead of declaring a second-instance failure.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "WeFlow.exe"
            exe.write_text("", encoding="utf-8")
            config_path = root / "config.toml"
            config_path.write_text(
                f"""
[automation]
driver = "cdp"
weflow_exe = "{exe.as_posix()}"
electron_cdp_port = 9333
cdp_busy_timeout_sec = 5
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            # 1st False = fast-path probe; 2nd False then True = busy-wait loop recovering.
            with (
                patch(
                    "wechat_diary_core.backends.weflow.launcher.is_cdp_available",
                    side_effect=[False, False, True],
                ),
                patch("wechat_diary_core.backends.weflow.launcher.is_weflow_process_running", return_value=True),
                patch("wechat_diary_core.backends.weflow.launcher.launch_weflow") as launch,
                patch("wechat_diary_core.backends.weflow.launcher.normalize_weflow_window", return_value=True),
                patch("wechat_diary_core.backends.weflow.launcher.find_weflow_windows", return_value=()),
            ):
                session = ensure_weflow_running(cfg)

        self.assertFalse(session.process_started)
        self.assertEqual(session.cdp_endpoint, "http://127.0.0.1:9333")
        launch.assert_not_called()

    def test_restart_weflow_requires_all_old_processes_to_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "WeFlow.exe"
            exe.write_text("", encoding="utf-8")
            config_path = root / "config.toml"
            config_path.write_text(
                f"""
[automation]
driver = "cdp"
weflow_exe = "{exe.as_posix()}"
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            with (
                patch("wechat_diary_core.backends.weflow.launcher.stop_weflow_processes", return_value=False),
                patch("wechat_diary_core.backends.weflow.launcher.ensure_weflow_running") as ensure,
            ):
                with self.assertRaises(WeFlowLaunchTimeout):
                    restart_weflow(cfg)

        ensure.assert_not_called()

    def test_find_weflow_windows_ignores_non_weflow_processes_with_matching_titles(self) -> None:
        fake_user32 = FakeUser32(
            [
                (1, "WeFlow-raw-exports", 100, True),
                (2, "WeFlow", 200, True),
            ]
        )

        def image_name(pid: int) -> str | None:
            return {100: "explorer.exe", 200: "WeFlow.exe"}.get(pid)

        with (
            patch("wechat_diary_core.backends.weflow.launcher.ctypes.windll", create=True) as windll,
            patch(
                "wechat_diary_core.backends.weflow.launcher.ctypes.WINFUNCTYPE",
                create=True,
                side_effect=lambda *_args: (lambda callback: callback),
            ),
        ):
            windll.user32 = fake_user32
            windows = find_weflow_windows(process_image_name_func=image_name)

        self.assertEqual(windows, (WeFlowWindow(pid=200, image_name="WeFlow.exe", title="WeFlow"),))

    def test_find_weflow_window_skips_non_weflow_titled_windows(self) -> None:
        # normalize_weflow_window MoveWindow's whatever this returns; a title
        # match alone would pick an Explorer "WeFlow-raw-exports" window and
        # resize the user's file browser. Must return the real WeFlow.exe hwnd.
        fake_user32 = FakeUser32(
            [
                (1, "WeFlow-raw-exports", 100, True),
                (2, "WeFlow", 200, True),
            ]
        )

        def image_name(pid: int) -> str | None:
            return {100: "explorer.exe", 200: "WeFlow.exe"}.get(pid)

        with (
            patch("wechat_diary_core.backends.weflow.launcher.ctypes.windll", create=True) as windll,
            patch(
                "wechat_diary_core.backends.weflow.launcher.ctypes.WINFUNCTYPE",
                create=True,
                side_effect=lambda *_args: (lambda callback: callback),
            ),
        ):
            windll.user32 = fake_user32
            hwnd = _find_weflow_window(process_image_name_func=image_name)

        self.assertEqual(hwnd, 2)

    def test_assert_single_weflow_instance_rejects_multiple_visible_weflow_processes(self) -> None:
        session = WeFlowSession(
            driver="cdp",
            cdp_endpoint="http://127.0.0.1:9222",
            process_started=True,
            process_id=100,
            window_normalized=True,
            window_process_ids=(100,),
        )

        windows = (
            WeFlowWindow(pid=100, image_name="WeFlow.exe", title="WeFlow"),
            WeFlowWindow(pid=200, image_name="WeFlow.exe", title="WeFlow"),
        )
        with patch("wechat_diary_core.backends.weflow.launcher.find_weflow_windows", return_value=windows):
            with self.assertRaises(WeFlowInstanceConflict) as captured:
                assert_single_weflow_instance(session)

        message = str(captured.exception)
        self.assertIn("pid=100", message)
        self.assertIn("pid=200", message)
        self.assertIn("image=WeFlow.exe", message)
        self.assertIn("title='WeFlow'", message)


if __name__ == "__main__":
    unittest.main()
