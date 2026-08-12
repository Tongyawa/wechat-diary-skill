from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import process_existing_raw, run_daily_export
from wechat_diary_core.config import load_config
from wechat_diary_core.workspace_discovery import (
    WORKSPACE_ENV_VAR,
    WorkspaceResolutionError,
    resolve_config_path,
)


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


def _workspace_from_example(root: Path, marker: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    body = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    replacements = {
        'raw = "WeFlow-raw-exports"': f'raw = "raw-{marker}"',
        'processed = "WeFlow-processed-exports"': f'processed = "processed-{marker}"',
        'archived = "WeFlow-archived-exports"': f'archived = "archived-{marker}"',
        'insights = "WeFlow-insights"': f'insights = "insights-{marker}"',
        'backend = "weflow_api"': 'backend = "manual"',
    }
    for original, replacement in replacements.items():
        if original not in body:
            raise AssertionError(f"config.example.toml 缺少预期模板行：{original}")
        body = body.replace(original, replacement, 1)
    (root / "config.toml").write_text(body, encoding="utf-8")
    for name in (f"raw-{marker}", f"processed-{marker}", f"archived-{marker}", f"insights-{marker}"):
        (root / name).mkdir()
    return root


@contextlib.contextmanager
def _working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class WorkspaceDiscoveryTests(unittest.TestCase):
    def test_explicit_config_wins_over_cwd_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explicit = _workspace_from_example(root / "explicit", "explicit")
            cwd = _workspace_from_example(root / "cwd", "cwd")
            environment = _workspace_from_example(root / "environment", "environment")

            resolved = resolve_config_path(
                explicit / "config.toml",
                cwd=cwd,
                environ={WORKSPACE_ENV_VAR: str(environment)},
            )
            cfg = load_config(resolved)

        self.assertEqual(resolved, (explicit / "config.toml").resolve())
        self.assertEqual(cfg.paths.raw, (explicit / "raw-explicit").resolve())

    def test_cwd_config_wins_even_when_environment_points_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = _workspace_from_example(root / "cwd", "cwd")
            environment = _workspace_from_example(root / "environment", "environment")

            resolved = resolve_config_path(
                cwd=cwd,
                environ={WORKSPACE_ENV_VAR: str(environment)},
            )
            cfg = load_config(resolved)

        self.assertEqual(resolved, (cwd / "config.toml").resolve())
        self.assertEqual(cfg.paths.raw, (cwd / "raw-cwd").resolve())
        self.assertNotEqual(cfg.paths.raw, (environment / "raw-environment").resolve())

    def test_environment_workspace_is_used_when_cwd_has_no_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "unrelated"
            cwd.mkdir()
            environment = _workspace_from_example(root / "environment", "environment")

            resolved = resolve_config_path(
                cwd=cwd,
                environ={WORKSPACE_ENV_VAR: str(environment)},
            )
            cfg = load_config(resolved)

        self.assertEqual(resolved, (environment / "config.toml").resolve())
        self.assertEqual(cfg.paths.raw, (environment / "raw-environment").resolve())

    def test_all_discovery_candidates_missing_never_loads_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "unrelated"
            cwd.mkdir()
            environment = root / "environment"
            stderr = io.StringIO()

            with _working_directory(cwd), mock.patch.dict(
                os.environ, {WORKSPACE_ENV_VAR: str(environment)}, clear=False
            ), mock.patch.object(process_existing_raw, "load_config") as load_mock, contextlib.redirect_stderr(stderr):
                exit_code = process_existing_raw.main([])

        self.assertEqual(exit_code, 2)
        load_mock.assert_not_called()
        message = stderr.getvalue()
        for path in ((cwd / "config.toml").resolve(), (environment / "config.toml").resolve()):
            self.assertIn(str(path), message)
        self.assertIn(WORKSPACE_ENV_VAR, message)
        self.assertIn("cd", message)
        self.assertIn("--config", message)

    def test_explicit_missing_config_is_hard_failure_without_cwd_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = _workspace_from_example(root / "cwd", "must-not-load")
            explicit = root / "missing-explicit.toml"
            env = os.environ.copy()
            env.pop(WORKSPACE_ENV_VAR, None)

            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "doctor.py"), "--config", str(explicit), "--json"],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )

        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, combined)
        self.assertIn(str(explicit.resolve()), completed.stderr)
        self.assertNotIn("raw-must-not-load", combined)
        self.assertNotIn(str((cwd / "config.toml").resolve()), combined)

    def test_daily_explicit_missing_config_bootstraps_but_implicit_missing_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explicit_workspace = root / "explicit-workspace"
            explicit_workspace.mkdir()
            explicit_config = explicit_workspace / "config.toml"
            explicit_stdout = io.StringIO()
            explicit_stderr = io.StringIO()

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(WORKSPACE_ENV_VAR, None)
                with contextlib.redirect_stdout(explicit_stdout), contextlib.redirect_stderr(explicit_stderr):
                    explicit_exit = run_daily_export.main(
                        ["--config", str(explicit_config), "--no-config-prompt"]
                    )

            implicit_workspace = root / "implicit-workspace"
            implicit_workspace.mkdir()
            implicit_stdout = io.StringIO()
            implicit_stderr = io.StringIO()
            with _working_directory(implicit_workspace), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(WORKSPACE_ENV_VAR, None)
                with contextlib.redirect_stdout(implicit_stdout), contextlib.redirect_stderr(implicit_stderr):
                    implicit_exit = run_daily_export.main(["--no-config-prompt"])

            explicit_exists = explicit_config.is_file()
            implicit_exists = (implicit_workspace / "config.toml").exists()

        self.assertEqual(explicit_exit, 1)
        self.assertTrue(explicit_exists)
        self.assertIn(f"Created local config: {explicit_config}", explicit_stdout.getvalue())
        self.assertEqual(implicit_exit, 2)
        self.assertFalse(implicit_exists)
        self.assertIn("找不到 config.toml", implicit_stderr.getvalue())

    def test_resolver_exception_exposes_probes_without_creating_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(WorkspaceResolutionError) as caught:
                resolve_config_path(cwd=root, environ={})

        self.assertEqual(caught.exception.probes, (("当前目录", (root / "config.toml").resolve()),))

    def test_doctor_json_from_unrelated_directory_uses_environment_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unrelated = root / "unrelated"
            unrelated.mkdir()
            workspace = _workspace_from_example(root / "workspace", "doctor")
            env = os.environ.copy()
            env[WORKSPACE_ENV_VAR] = str(workspace)

            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "doctor.py"), "--json"],
                cwd=unrelated,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )
            inside_env = env.copy()
            inside_env.pop(WORKSPACE_ENV_VAR, None)
            inside = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "doctor.py"), "--json"],
                cwd=workspace,
                env=inside_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )

        self.assertNotEqual(completed.returncode, 2, completed.stderr)
        self.assertNotEqual(inside.returncode, 2, inside.stderr)
        payload = json.loads(completed.stdout)
        inside_payload = json.loads(inside.stdout)
        raw_check = next(check for check in payload["checks"] if check["id"] == "path_raw")
        inside_raw_check = next(check for check in inside_payload["checks"] if check["id"] == "path_raw")
        self.assertEqual(raw_check["details"]["path"], str((workspace / "raw-doctor").resolve()))
        self.assertEqual(raw_check, inside_raw_check)
        self.assertEqual(payload["summary"], inside_payload["summary"])
        self.assertNotEqual(raw_check["details"]["path"], str((unrelated / "WeFlow-raw-exports").resolve()))


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "需要 Windows PowerShell 5.1")
class PowerShellWorkspaceDiscoveryTests(unittest.TestCase):
    def _run_latest(
        self,
        cwd: Path,
        env: dict[str, str],
        *args: str,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                str(POWERSHELL),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "Open-LatestInsights.ps1"),
                "-NoOpen",
                "-NoPause",
                *args,
            ],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )

    def _run_daily(
        self,
        cwd: Path,
        env: dict[str, str],
        workspace: Path,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                str(POWERSHELL),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "run_daily_export.ps1"),
                "-Workspace",
                str(workspace),
                "-NoPause",
            ],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )

    @staticmethod
    def _decode(data: bytes) -> str:
        return "\n".join(data.decode(encoding, errors="replace") for encoding in ("utf-8", "gb18030", "cp936"))

    def test_environment_workspace_and_missing_error_in_powershell_51(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unrelated = root / "unrelated"
            unrelated.mkdir()
            workspace = _workspace_from_example(root / "workspace", "powershell")
            env = os.environ.copy()
            env[WORKSPACE_ENV_VAR] = f"  {workspace}  "

            success = self._run_latest(unrelated, env)
            success_output = self._decode(success.stdout + b"\n" + success.stderr)
            self.assertEqual(success.returncode, 0, success_output)
            self.assertIn(str((workspace / "insights-powershell").resolve()), success_output)

            missing_workspace = root / "missing-workspace"
            env[WORKSPACE_ENV_VAR] = str(missing_workspace)
            failure = self._run_latest(unrelated, env)
            failure_output = self._decode(failure.stdout + b"\n" + failure.stderr)

        self.assertEqual(failure.returncode, 2, failure_output)
        self.assertIn("找不到 config.toml", failure_output)
        self.assertIn(str((unrelated / "config.toml").resolve()), failure_output)
        self.assertIn(str((missing_workspace / "config.toml").resolve()), failure_output)
        self.assertIn(WORKSPACE_ENV_VAR, failure_output)
        self.assertIn("-Workspace", failure_output)

    def test_environment_workspace_smoke_from_arbitrary_directory_across_entrypoints(self) -> None:
        """文档承诺的环境变量发现必须同时穿过 Python 与 ps1 真入口。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unrelated = root / "unrelated"
            unrelated.mkdir()
            workspace = _workspace_from_example(root / "workspace", "smoke")
            env = os.environ.copy()
            env[WORKSPACE_ENV_VAR] = str(workspace)

            doctor = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "doctor.py"), "--json"],
                cwd=unrelated,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )
            latest = self._run_latest(unrelated, env)
            latest_output = self._decode(latest.stdout + b"\n" + latest.stderr)

        self.assertNotEqual(2, doctor.returncode, doctor.stdout + doctor.stderr)
        self.assertEqual(0, latest.returncode, latest_output)
        payload = json.loads(doctor.stdout)
        raw_check = next(check for check in payload["checks"] if check["id"] == "path_raw")
        self.assertEqual(
            raw_check["details"]["path"],
            str((workspace / "raw-smoke").resolve()),
        )
        self.assertIn(str((workspace / "insights-smoke").resolve()), latest_output)

    def test_daily_explicit_workspace_bootstraps_config_in_powershell_51(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unrelated = root / "unrelated"
            unrelated.mkdir()
            workspace = root / "empty-workspace"
            workspace.mkdir()
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            (fake_bin / "python.cmd").write_text(
                f'@echo off\r\n"{sys.executable}" %* --no-config-prompt\r\n',
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.pop(WORKSPACE_ENV_VAR, None)
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")

            completed = self._run_daily(unrelated, env, workspace)
            output = self._decode(completed.stdout + b"\n" + completed.stderr)
            config_exists = (workspace / "config.toml").is_file()

        self.assertEqual(completed.returncode, 1, output)
        self.assertTrue(config_exists, output)
        self.assertIn("Created local config:", output)
        self.assertIn("config.toml 缺少 [export_backend.weflow_api].access_token", output)

    def test_explicit_missing_workspace_does_not_fall_back_to_valid_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = _workspace_from_example(root / "cwd", "ps-must-not-load")
            missing_workspace = root / "missing-workspace"
            env = os.environ.copy()
            env.pop(WORKSPACE_ENV_VAR, None)

            completed = self._run_latest(cwd, env, "-Workspace", str(missing_workspace))
            output = self._decode(completed.stdout + b"\n" + completed.stderr)

        self.assertEqual(completed.returncode, 2, output)
        self.assertIn(str((missing_workspace / "config.toml").resolve()), output)
        self.assertNotIn("insights-ps-must-not-load", output)
        self.assertNotIn(str((cwd / "config.toml").resolve()), output)

    def test_open_latest_malformed_config_uses_documented_clean_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "config.toml").write_text("[paths\ninvalid", encoding="utf-8")
            env = os.environ.copy()
            env.pop(WORKSPACE_ENV_VAR, None)

            completed = self._run_latest(root, env, "-Workspace", str(workspace))
            output = self._decode(completed.stdout + b"\n" + completed.stderr)
            fallback = workspace / "WeFlow-insights"

        self.assertEqual(completed.returncode, 0, output)
        self.assertIn(f"已使用回退路径：{fallback}", output)
        self.assertIn("请对照 config.example.toml 修正配置后重试", output)
        self.assertNotIn("CategoryInfo", output)
        self.assertNotIn("~~~~", output)

    def test_open_by_date_explicit_insights_root_bypasses_workspace_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unrelated = root / "unrelated"
            unrelated.mkdir()
            insights = root / "direct-insights"
            insights.mkdir()
            env = os.environ.copy()
            env.pop(WORKSPACE_ENV_VAR, None)

            completed = subprocess.run(
                [
                    str(POWERSHELL),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "Open-InsightsByDate.ps1"),
                    "-Date",
                    "2026-08-12",
                    "-InsightsRoot",
                    str(insights),
                    "-NoOpen",
                    "-NoPause",
                ],
                cwd=unrelated,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
            output = self._decode(completed.stdout + b"\n" + completed.stderr)

        self.assertEqual(completed.returncode, 0, output)
        self.assertIn(f"二次加工产物根目录：{insights}", output)
        self.assertNotIn("找不到 config.toml", output)


if __name__ == "__main__":
    unittest.main()
