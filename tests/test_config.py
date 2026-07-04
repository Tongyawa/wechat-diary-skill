from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wechat_diary_core.config import load_config


class ConfigTests(unittest.TestCase):
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
