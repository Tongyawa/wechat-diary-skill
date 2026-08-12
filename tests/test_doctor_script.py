from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.doctor import CheckResult, DoctorReport, ProbeDependencies, main, run_doctor


def _write_config(
    root: Path,
    *,
    executable_exists: bool = True,
    create_roots: bool = True,
    image_ocr_enabled: bool = True,
    voice_users: bool = True,
    fallback: str = "",
) -> Path:
    executable = root / "WeFlow.exe"
    if executable_exists:
        executable.write_text("placeholder", encoding="utf-8")

    data_roots = {name: root / name for name in ("raw", "processed", "archived", "insights")}
    if create_roots:
        for path in data_roots.values():
            path.mkdir(exist_ok=True)

    config = root / "config.toml"
    config.write_text(
        f"""
[user]
voice_transcribe_usernames = {['Contact'] if voice_users else []}

[paths]
raw = "{data_roots['raw'].as_posix()}"
processed = "{data_roots['processed'].as_posix()}"
archived = "{data_roots['archived'].as_posix()}"
insights = "{data_roots['insights'].as_posix()}"

[automation]
weflow_exe = "{executable.as_posix()}"
electron_cdp_port = 9222

[preprocessing]
image_ocr_enabled = {str(image_ocr_enabled).lower()}

[daily_export]
voice_fallback_script = "{fallback}"
""".strip(),
        encoding="utf-8",
    )
    return config


def _deps(*, cdp_ready: bool = True, ocr_ready: bool = True, writable: bool = True) -> ProbeDependencies:
    def fetch(endpoint: str) -> list[dict[str, object]]:
        if not cdp_ready:
            raise OSError("connection refused")
        return [{"type": "page", "url": "#/home"}]

    return ProbeDependencies(
        fetch_cdp_targets=fetch,
        find_spec=lambda name: object() if ocr_ready and name == "rapidocr_onnxruntime" else None,
        can_write=lambda path: writable,
    )


def _by_id(report: DoctorReport, check_id: str) -> CheckResult:
    return next(check for check in report.checks if check.id == check_id)


class DoctorTests(unittest.TestCase):
    def test_weflow_api_backend_has_health_token_semantic_and_optional_asr_checks(self) -> None:
        class ApiClient:
            def health(self):
                return {"status": "ok"}

            def semantic_probe(self):
                return "wxid_placeholder", 1

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("raw", "processed", "archived", "insights"):
                (root / name).mkdir()
            config = root / "config.toml"
            config.write_text(
                f"""
[paths]
raw = "{(root / 'raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
archived = "{(root / 'archived').as_posix()}"
insights = "{(root / 'insights').as_posix()}"

[export_backend]
backend = "weflow_api"

[export_backend.weflow_api]
base_url = "http://127.0.0.1:5031"
access_token = "fixed-token"

[asr]
engine = "sensevoice"
worker_python = "{Path(sys.executable).as_posix()}"
""".strip(),
                encoding="utf-8",
            )
            deps = ProbeDependencies(
                api_client_factory=lambda cfg: ApiClient(),
                find_spec=lambda name: object(),
                can_write=lambda path: True,
            )
            report = run_doctor(config, deps=deps)

        for check_id in (
            "weflow_api_message_timeout",
            "weflow_api_health",
            "weflow_api_token",
            "weflow_api_semantic",
            "dependency_asr",
        ):
            self.assertEqual(_by_id(report, check_id).status, "ready", check_id)

    def test_explicit_low_message_timeout_warns_with_exact_config_action(self) -> None:
        class ApiClient:
            def health(self):
                return {"status": "ok"}

            def semantic_probe(self):
                return "wxid_placeholder", 1

        for seconds, expected_status in ((120, "warning"), (600, "ready")):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for name in ("raw", "processed", "archived", "insights"):
                    (root / name).mkdir()
                config = root / "config.toml"
                config.write_text(
                    f'''
[paths]
raw = "{(root / 'raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
archived = "{(root / 'archived').as_posix()}"
insights = "{(root / 'insights').as_posix()}"

[export_backend]
backend = "weflow_api"

[export_backend.weflow_api]
base_url = "http://127.0.0.1:5031"
access_token = "token-placeholder"
message_request_timeout_sec = {seconds}
'''.strip(),
                    encoding="utf-8",
                )
                report = run_doctor(
                    config,
                    deps=ProbeDependencies(
                        api_client_factory=lambda cfg: ApiClient(),
                        find_spec=lambda name: None,
                        can_write=lambda path: True,
                    ),
                )

            check = _by_id(report, "weflow_api_message_timeout")
            self.assertEqual(check.status, expected_status)
            if seconds < 300:
                self.assertIn("104 秒以上", check.message)
                self.assertIn("冷缓存", check.message)
                self.assertIn("message_request_timeout_sec", check.action)
                self.assertIn("600–900", check.action)

    def test_weflow_api_asr_missing_is_warning_not_error(self) -> None:
        class ApiClient:
            def health(self):
                return {"status": "ok"}

            def semantic_probe(self):
                return "wxid_placeholder", 1

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root)
            body = config.read_text(encoding="utf-8") + """

[export_backend]
backend = "weflow_api"

[export_backend.weflow_api]
base_url = "http://127.0.0.1:5031"
access_token = "fixed-token"

[asr]
engine = "sensevoice"
"""
            config.write_text(body, encoding="utf-8")
            report = run_doctor(
                config,
                deps=ProbeDependencies(
                    api_client_factory=lambda cfg: ApiClient(),
                    find_spec=lambda name: None,
                    can_write=lambda path: True,
                ),
            )

        self.assertEqual(_by_id(report, "dependency_asr").status, "warning")
        self.assertNotIn("dependency_asr", [check.id for check in report.checks if check.status == "error"])

    def test_weflow_api_asr_invalid_worker_python_has_clear_warning(self) -> None:
        class ApiClient:
            def health(self):
                return {"status": "ok"}

            def semantic_probe(self):
                return "wxid_placeholder", 1

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("raw", "processed", "archived", "insights"):
                (root / name).mkdir()
            config = root / "config.toml"
            config.write_text(
                f'''
[paths]
raw = "{(root / 'raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
archived = "{(root / 'archived').as_posix()}"
insights = "{(root / 'insights').as_posix()}"

[export_backend]
backend = "weflow_api"

[export_backend.weflow_api]
access_token = "fixed-token"

[asr]
engine = "sensevoice"
worker_python = "missing/python.exe"
'''.strip(),
                encoding="utf-8",
            )
            report = run_doctor(
                config,
                deps=ProbeDependencies(
                    api_client_factory=lambda cfg: ApiClient(),
                    find_spec=lambda name: None,
                    can_write=lambda path: True,
                ),
            )

        check = _by_id(report, "dependency_asr")
        self.assertEqual(check.status, "warning")
        self.assertIn("不存在或不可执行", check.message)
        self.assertIn("worker_python", check.action)

    def test_ready_state_covers_all_core_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = run_doctor(_write_config(Path(tmp)), deps=_deps())

        for check_id in (
            "config",
            "weflow_executable",
            "cdp",
            "path_raw",
            "path_processed",
            "path_archived",
            "path_insights",
            "dependency_ocr",
            "dependency_voice_transcribe",
        ):
            self.assertEqual(_by_id(report, check_id).status, "ready", check_id)
        self.assertTrue(report.to_dict()["summary"]["can_run_daily_export"])
        self.assertEqual(report.to_dict()["summary"]["conclusion"], "可以跑每日导出")

    def test_missing_config_gives_copy_action_and_skips_dependent_probes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = run_doctor(Path(tmp) / "missing.toml", deps=_deps())

        config = _by_id(report, "config")
        self.assertEqual(config.status, "error")
        self.assertIn("复制 config.example.toml 为 config.toml", config.action or "")
        self.assertEqual(_by_id(report, "cdp").status, "warning")

    def test_missing_critical_config_key_is_not_hidden_by_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root)
            body = config.read_text(encoding="utf-8").replace("electron_cdp_port = 9222\n", "")
            config.write_text(body, encoding="utf-8")
            report = run_doctor(config, deps=_deps())

        result = _by_id(report, "config")
        self.assertEqual(result.status, "error")
        self.assertIn("export_backend.weflow.electron_cdp_port", result.message)
        self.assertIn("config.example.toml", result.action or "")

    def test_missing_weflow_executable_gives_config_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = run_doctor(_write_config(Path(tmp), executable_exists=False), deps=_deps())

        result = _by_id(report, "weflow_executable")
        self.assertEqual(result.status, "error")
        self.assertIn("export_backend.weflow.weflow_exe", result.action or "")

    def test_manual_backend_skips_weflow_and_cdp_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, executable_exists=False)
            body = config.read_text(encoding="utf-8") + '\n\n[export_backend]\nbackend = "manual"\n'
            body = body.replace("electron_cdp_port = 9222\n", "")
            config.write_text(body, encoding="utf-8")
            report = run_doctor(config, deps=_deps(cdp_ready=False))

        self.assertEqual(_by_id(report, "config").status, "ready")
        self.assertEqual(_by_id(report, "weflow_executable").status, "ready")
        self.assertEqual(_by_id(report, "cdp").status, "ready")
        self.assertTrue(report.to_dict()["summary"]["can_run_daily_export"])

    def test_unavailable_cdp_is_a_warning_with_launch_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = run_doctor(_write_config(Path(tmp)), deps=_deps(cdp_ready=False))

        result = _by_id(report, "cdp")
        self.assertEqual(result.status, "warning")
        self.assertIn("双击 Start-DailyExport.bat 会自动拉起", result.action or "")
        self.assertTrue(report.to_dict()["summary"]["can_run_daily_export"])

    def test_missing_and_unwritable_data_roots_have_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_report = run_doctor(_write_config(root, create_roots=False), deps=_deps())
            for check_id in ("path_raw", "path_processed", "path_archived", "path_insights"):
                self.assertEqual(_by_id(missing_report, check_id).status, "error")

            self.assertIn("夸克网盘同步", _by_id(missing_report, "path_archived").action or "")
            for name in ("raw", "processed", "archived", "insights"):
                (root / name).mkdir()
            unwritable_report = run_doctor(root / "config.toml", deps=_deps(writable=False))

        self.assertEqual(_by_id(unwritable_report, "path_raw").status, "error")
        self.assertIn("写入权限", _by_id(unwritable_report, "path_raw").action or "")

    def test_relative_persistent_root_uses_generic_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, create_roots=False)
            body = config.read_text(encoding="utf-8").replace(
                f'archived = "{(root / "archived").as_posix()}"',
                'archived = "archived"',
            )
            config.write_text(body, encoding="utf-8")
            report = run_doctor(config, deps=_deps())

        action = _by_id(report, "path_archived").action or ""
        self.assertIn("paths.archived", action)
        self.assertNotIn("夸克", action)

    def test_ocr_dependency_ready_and_missing_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _write_config(Path(tmp))
            ready = run_doctor(config, deps=_deps(ocr_ready=True))
            missing = run_doctor(config, deps=_deps(ocr_ready=False))

        self.assertEqual(_by_id(ready, "dependency_ocr").status, "ready")
        self.assertEqual(_by_id(missing, "dependency_ocr").status, "error")
        self.assertIn("pip install -r requirements.txt", _by_id(missing, "dependency_ocr").action or "")

    def test_voice_capabilities_cover_disabled_and_fallback_file_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            disabled = run_doctor(_write_config(root, voice_users=False), deps=_deps())
            self.assertEqual(_by_id(disabled, "dependency_voice_transcribe").status, "warning")

            fallback = root / "fallback.py"
            config = _write_config(root, fallback=fallback.as_posix())
            missing = run_doctor(config, deps=_deps())
            self.assertEqual(_by_id(missing, "dependency_voice_fallback").status, "error")
            fallback.write_text("# placeholder", encoding="utf-8")
            ready = run_doctor(config, deps=_deps())

        self.assertEqual(_by_id(ready, "dependency_voice_fallback").status, "ready")

    def test_json_mode_outputs_only_stable_structure(self) -> None:
        report = DoctorReport(
            [CheckResult("config", "配置", "config.toml", "ready", "ok")]
        )
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text("", encoding="utf-8")
            stdout = io.StringIO()
            with patch("scripts.doctor.run_doctor", return_value=report), contextlib.redirect_stdout(stdout):
                exit_code = main(["--config", str(config), "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["checks"][0]["id"], "config")
        self.assertEqual(
            payload["summary"],
            {
                "ready": 1,
                "warning": 0,
                "error": 0,
                "can_run_daily_export": True,
                "conclusion": "可以跑每日导出",
            },
        )


if __name__ == "__main__":
    unittest.main()
