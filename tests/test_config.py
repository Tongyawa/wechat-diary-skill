from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from wechat_diary_core.config import load_config


class ConfigTests(unittest.TestCase):
    def test_legacy_automation_maps_to_weflow_backend_with_one_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[automation]
driver = "uia"
electron_cdp_port = 9333
""".strip(),
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stderr(output):
                first = load_config(config_path)
                second = load_config(config_path)

        self.assertEqual(first.export_backend.backend, "weflow")
        self.assertEqual(first.export_backend.weflow.driver, "uia")
        self.assertEqual(first.export_backend.weflow.electron_cdp_port, 9333)
        self.assertIs(first.automation, first.export_backend.weflow)
        self.assertEqual(second.automation.driver, "uia")
        self.assertEqual(output.getvalue().count("config 建议迁移到 [export_backend.weflow]"), 1)

    def test_new_backend_config_takes_precedence_without_migration_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[export_backend]
backend = "manual"

[export_backend.weflow]
driver = "template"
""".strip(),
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                cfg = load_config(config_path)

        self.assertEqual(cfg.export_backend.backend, "manual")
        self.assertEqual(cfg.automation.driver, "template")
        self.assertEqual(output.getvalue(), "")

    def test_load_config_merges_defaults_and_resolves_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[automation]
driver = "uia"

[paths]
raw = "raw"
""".strip(),
                encoding="utf-8",
            )

            cfg = load_config(config_path)

        self.assertEqual(cfg.automation.driver, "uia")
        self.assertTrue(str(cfg.paths.raw).endswith("raw"))
        self.assertEqual(cfg.paths.processed.name, "WeFlow-processed-exports")
        self.assertTrue(cfg.preprocessing.group_context_window.enabled)
        self.assertEqual(cfg.preprocessing.group_context_window.messages_before, 3)
        self.assertEqual(cfg.preprocessing.group_context_window.messages_after, 5)
        self.assertEqual(cfg.preprocessing.group_context_window.time_window_minutes, 15)
        self.assertEqual(cfg.preprocessing.group_context_window.anchor_keywords, [])
        self.assertEqual(cfg.skills.daily, ["wechat-diary-skill"])
        self.assertEqual(cfg.daily_export.target_usernames, [])
        self.assertEqual(cfg.daily_export.self_moments_usernames, [])
        self.assertFalse(cfg.daily_export.self_moments_configured)
        self.assertEqual(cfg.daily_export.target_processed_subroot, "_targets")
        self.assertEqual(cfg.daily_export.cleanup_mode, "archive")
        self.assertTrue(cfg.daily_export.restart_weflow)
        self.assertEqual(cfg.paths.archived.name, "WeFlow-archived-exports")
        self.assertEqual(cfg.source["automation"]["driver"], "uia")
        self.assertEqual(cfg.source["paths"]["raw"], "raw")
        self.assertNotIn("processed", cfg.source["paths"])

    def test_load_config_reads_daily_export_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[daily_export]
target_usernames = ["wxid_target"]
self_moments_usernames = ["wxid_self"]
target_processed_subroot = "_sidecar"
cleanup_mode = "delete"
restart_weflow = false
""".strip(),
                encoding="utf-8",
            )

            cfg = load_config(config_path)

        self.assertEqual(cfg.daily_export.target_usernames, ["wxid_target"])
        self.assertEqual(cfg.daily_export.self_moments_usernames, ["wxid_self"])
        self.assertTrue(cfg.daily_export.self_moments_configured)
        self.assertEqual(cfg.daily_export.target_processed_subroot, "_sidecar")
        self.assertEqual(cfg.daily_export.cleanup_mode, "delete")
        self.assertFalse(cfg.daily_export.restart_weflow)

    def test_load_config_can_disable_group_context_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[preprocessing.group_context_window]
enabled = false
""".strip(),
                encoding="utf-8",
            )

            cfg = load_config(config_path)

        self.assertFalse(cfg.preprocessing.group_context_window.enabled)

    def test_load_config_treats_explicit_empty_self_moments_as_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[daily_export]
self_moments_usernames = []
""".strip(),
                encoding="utf-8",
            )

            cfg = load_config(config_path)

        self.assertEqual(cfg.daily_export.self_moments_usernames, [])
        self.assertTrue(cfg.daily_export.self_moments_configured)


if __name__ == "__main__":
    unittest.main()
