from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_diary_core.config import load_config
from wechat_diary_core.weflow_automation.launcher import (
    WeFlowInstanceConflict,
    WeFlowLaunchTimeout,
    WeFlowSession,
    assert_single_weflow_instance,
    build_launch_args,
    cdp_endpoint_url,
    ensure_weflow_running,
    restart_weflow,
    stop_weflow_processes,
)


class LauncherTests(unittest.TestCase):
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

    def test_stop_weflow_processes_uses_taskkill_when_running(self) -> None:
        with (
            patch("wechat_diary_core.weflow_automation.launcher.is_weflow_process_running", side_effect=[True, False]),
            patch("wechat_diary_core.weflow_automation.launcher.subprocess.run") as run,
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
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            with (
                patch("wechat_diary_core.weflow_automation.launcher.is_cdp_available", return_value=False),
                patch("wechat_diary_core.weflow_automation.launcher.is_weflow_process_running", return_value=True),
                patch("wechat_diary_core.weflow_automation.launcher.launch_weflow") as launch,
            ):
                with self.assertRaises(WeFlowLaunchTimeout):
                    ensure_weflow_running(cfg)

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
                patch("wechat_diary_core.weflow_automation.launcher.stop_weflow_processes", return_value=False),
                patch("wechat_diary_core.weflow_automation.launcher.ensure_weflow_running") as ensure,
            ):
                with self.assertRaises(WeFlowLaunchTimeout):
                    restart_weflow(cfg)

        ensure.assert_not_called()

    def test_assert_single_weflow_instance_rejects_multiple_visible_windows(self) -> None:
        session = WeFlowSession(
            driver="cdp",
            cdp_endpoint="http://127.0.0.1:9222",
            process_started=True,
            process_id=100,
            window_normalized=True,
            window_process_ids=(100,),
        )

        with patch("wechat_diary_core.weflow_automation.launcher.find_weflow_window_process_ids", return_value={100, 200}):
            with self.assertRaises(WeFlowInstanceConflict):
                assert_single_weflow_instance(session)


if __name__ == "__main__":
    unittest.main()
