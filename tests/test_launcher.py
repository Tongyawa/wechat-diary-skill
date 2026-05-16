from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_diary_core.config import load_config
from wechat_diary_core.weflow_automation.launcher import build_launch_args, cdp_endpoint_url, stop_weflow_processes


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


if __name__ == "__main__":
    unittest.main()
