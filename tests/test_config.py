from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from wechat_diary_core.config import WeflowApiConfig, load_config


class ConfigTests(unittest.TestCase):
    def test_fresh_defaults_select_weflow_api_and_disabled_asr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp) / "missing.toml")

        self.assertEqual(cfg.export_backend.backend, "weflow_api")
        self.assertEqual(cfg.export_backend.weflow_api.base_url, "http://127.0.0.1:5031")
        self.assertEqual(cfg.export_backend.weflow_api.access_token, "")
        self.assertTrue(cfg.export_backend.weflow_api.media_localize)
        self.assertEqual(cfg.export_backend.weflow_api.message_format, "json")
        self.assertEqual(cfg.export_backend.weflow_api.request_timeout_sec, 120)
        self.assertEqual(cfg.export_backend.weflow_api.message_request_timeout_sec, 600)
        self.assertEqual(cfg.export_backend.weflow_api.appmsg_text_max_chars, 300)
        self.assertEqual(cfg.asr.engine, "")
        self.assertIsNone(cfg.asr.worker_python)
        self.assertIsNone(cfg.asr.worker_script)
        self.assertEqual(cfg.asr.worker_startup_timeout_sec, 180)
        self.assertEqual(cfg.asr.worker_request_timeout_sec, 120)

    def test_reads_weflow_api_and_sensevoice_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[export_backend]
backend = "weflow_api"

[export_backend.weflow_api]
base_url = "http://127.0.0.1:6000/"
access_token = "fixed-token"
media_localize = false
message_format = "json"
request_timeout_sec = 42
message_request_timeout_sec = 642
appmsg_text_max_chars = 80

[asr]
engine = "sensevoice"
model = "local/model"
language = "yue"
device = "cpu"
emit_emotion = false
worker_python = "env/python.exe"
worker_script = "custom_worker.py"
worker_startup_timeout_sec = 240
worker_request_timeout_sec = 90
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)

        self.assertEqual(cfg.export_backend.backend, "weflow_api")
        self.assertEqual(cfg.export_backend.weflow_api.base_url, "http://127.0.0.1:6000")
        self.assertEqual(cfg.export_backend.weflow_api.access_token, "fixed-token")
        self.assertFalse(cfg.export_backend.weflow_api.media_localize)
        self.assertEqual(cfg.export_backend.weflow_api.request_timeout_sec, 42)
        self.assertEqual(cfg.export_backend.weflow_api.message_request_timeout_sec, 642)
        self.assertEqual(cfg.export_backend.weflow_api.appmsg_text_max_chars, 80)
        self.assertEqual(cfg.asr.engine, "sensevoice")
        self.assertEqual(cfg.asr.model, "local/model")
        self.assertEqual(cfg.asr.language, "yue")
        self.assertFalse(cfg.asr.emit_emotion)
        self.assertEqual(cfg.asr.worker_python, config_path.parent / "env" / "python.exe")
        self.assertEqual(cfg.asr.worker_script, config_path.parent / "custom_worker.py")
        self.assertEqual(cfg.asr.worker_startup_timeout_sec, 240)
        self.assertEqual(cfg.asr.worker_request_timeout_sec, 90)

    def test_weflow_api_config_old_constructor_args_keep_appmsg_default(self) -> None:
        cfg = WeflowApiConfig(
            base_url="http://127.0.0.1:5031",
            access_token="token-placeholder",
            media_localize=True,
            message_format="json",
            request_timeout_sec=120,
        )

        self.assertEqual(cfg.appmsg_text_max_chars, 300)
        self.assertEqual(cfg.message_request_timeout_sec, 600)

    def test_rejects_appmsg_text_limit_below_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                "[export_backend.weflow_api]\nappmsg_text_max_chars = 0",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "appmsg_text_max_chars 必须大于等于 1"):
                load_config(config_path)

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
        hint = output.getvalue()
        self.assertEqual(hint.count("config 建议迁移到 [export_backend.weflow]"), 1)
        self.assertIn(
            "请将 config.toml 的 [automation] / [automation.template_fallback] 段名改成 "
            "[export_backend.weflow] / [export_backend.weflow.template_fallback] 即可消除本提示。",
            hint,
        )

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
        self.assertTrue(cfg.daily_export.skip_official_accounts)
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
skip_official_accounts = false
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
        self.assertFalse(cfg.daily_export.skip_official_accounts)

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
