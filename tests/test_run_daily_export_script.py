from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.run_daily_export import (
    DailyExportDeps,
    DailyExportResult,
    DailyExportStageError,
    _review_session_failures,
    ensure_local_config,
    main,
    run_daily_export,
    wait_for_raw_exports_stable,
)
from wechat_diary_core.archiving import archive, archive_chats_for
from wechat_diary_core.config import load_config


@dataclass
class FakeBackend:
    """Configurable port fake used as the runner's executable specification."""

    calls: list[tuple] = field(default_factory=list)
    capabilities: frozenset[str] = frozenset({"moments", "voice_transcribe"})
    name: str = "fake"
    prepare_action: object | None = None
    export_chats_action: object | None = None
    export_moments_action: object | None = None
    transcribe_action: object | None = None
    shutdown_action: object | None = None

    def prepare(self) -> None:
        self.calls.append(("prepare_backend",))
        if callable(self.prepare_action):
            self.prepare_action()

    def export_chats(self, export_date: date) -> None:
        self.calls.append(("all_chats", export_date.isoformat()))
        if callable(self.export_chats_action):
            self.export_chats_action(export_date)

    def export_moments(self, usernames: list[str], export_date: date) -> None:
        self.calls.append(("moments", tuple(usernames)))
        if callable(self.export_moments_action):
            self.export_moments_action(usernames, export_date)

    def transcribe_voices(self, usernames: list[str]) -> None:
        self.calls.append(("voice", tuple(usernames)))
        if callable(self.transcribe_action):
            self.transcribe_action(usernames)

    def shutdown(self) -> None:
        self.calls.append(("shutdown_backend",))
        if callable(self.shutdown_action):
            self.shutdown_action()


def _quiet_deps(root: Path) -> DailyExportDeps:
    """Stub deps that run every stage as a no-op for output-focused tests."""
    return DailyExportDeps(
        backend=FakeBackend(),
        rotate_export_workspace=lambda cfg, label, mode: SimpleNamespace(target=None),
        wait_for_raw_exports_stable=lambda raw_path, min_files: None,
        run_voice_fallback_script=lambda script_path, config: None,
        archive=lambda raw_path, config, clear_first: [],
        archive_chats_for=lambda usernames, config, subroot, image_mode, clear_first: [],
        archive_moments_for=lambda usernames, config, subroot, clear_first: [],
    )


def _write_config(
    root: Path,
    *,
    target_users: str = '"Target"',
    self_users: str | None = "",
    voice_users: str = "",
    voice_fallback_script: str = "",
    backend: str | None = None,
) -> Path:
    config_path = root / "config.toml"
    # self_users=None omits the key entirely (the "never configured" state).
    self_moments_line = "" if self_users is None else f"self_moments_usernames = [{self_users}]\n"
    backend_section = "" if backend is None else f'\n[export_backend]\nbackend = "{backend}"\n'
    config_path.write_text(
        f"""
[user]
voice_transcribe_usernames = [{voice_users}]

[paths]
raw = "{(root / 'raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
archived = "{(root / 'archived').as_posix()}"

[automation]
weflow_exe = "{(root / 'WeFlow.exe').as_posix()}"

[daily_export]
target_usernames = [{target_users}]
{self_moments_line}target_processed_subroot = "_sidecar"
voice_fallback_script = "{voice_fallback_script}"
cleanup_mode = "archive"
restart_weflow = true
{backend_section}
""".strip(),
        encoding="utf-8",
    )
    return config_path


def _write_voice_failure_raw(root: Path, *, message_ids: list[int] | None = None) -> Path:
    ids = message_ids or [72, 73, 74]
    export_dir = root / "raw" / "Chat_20260516"
    export_dir.mkdir(parents=True)
    export_path = export_dir / "Chat_20260516.json"
    export_path.write_text(
        json.dumps(
            {
                "weflow": {},
                "session": {
                    "wxid": "Target",
                    "nickname": "Target",
                    "remark": "",
                    "displayName": "Target",
                    "type": "私聊",
                    "username": "Target",
                    "messageCount": len(ids),
                },
                "messages": [
                    {
                        "localId": local_id,
                        "createTime": 1778840000 + local_id,
                        "formattedTime": f"2026-05-16 10:{local_id % 60:02d}:00",
                        "type": "语音消息",
                        "content": "[语音消息 - 转文字失败: 未知错误]",
                        "source": "",
                        "isSend": 0,
                        "senderUsername": "Target",
                        "senderDisplayName": "Peer",
                        "platformMessageId": f"voice-{local_id}",
                    }
                    for local_id in ids
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return export_path


class DailyExportScriptTests(unittest.TestCase):
    def test_review_uses_tty_stdin_for_interactive_authorization(self) -> None:
        class TtyInput(io.StringIO):
            def isatty(self) -> bool:
                return True

        class ReviewBackend:
            def __init__(self) -> None:
                self.choice = ""
                self.interactive = False

            def review_session_failures(self, *, interactive, input_func) -> None:
                self.interactive = interactive
                self.choice = input_func("")

        backend = ReviewBackend()
        with patch("scripts.run_daily_export.sys.stdin", TtyInput("a\n")):
            _review_session_failures(backend)

        self.assertTrue(backend.interactive)
        self.assertEqual(backend.choice, "a")

    def test_wrappers_separate_code_root_from_workspace(self) -> None:
        daily = Path("scripts/run_daily_export.ps1").read_text(encoding="utf-8-sig")
        process = Path("scripts/process_existing_raw.ps1").read_text(encoding="utf-8-sig")
        latest = Path("scripts/Open-LatestInsights.ps1").read_text(encoding="utf-8-sig")
        by_date = Path("scripts/Open-InsightsByDate.ps1").read_text(encoding="utf-8-sig")
        batch = Path("Start-DailyExport.bat").read_text(encoding="utf-8")

        for script in (daily, process, latest, by_date):
            self.assertIn('[string]$Workspace = ""', script)
            self.assertIn("$CodeRoot", script)
            self.assertIn("$WorkspaceRoot", script)
        self.assertIn('Join-Path $WorkspaceRoot ".runlog"', daily)
        self.assertIn('Join-Path $WorkspaceRoot ".runlog"', process)
        self.assertNotIn('WeFlow-insights\\.runlog', daily)
        self.assertNotIn('WeFlow-insights\\.runlog', process)
        self.assertIn("if ($Line -match '^\\[INFO\\]')", daily)
        self.assertIn('if "%ROOT:~-1%"=="\\" set "ROOT=%ROOT:~0,-1%"', batch)
        self.assertIn('-File "%ROOT%\\scripts\\run_daily_export.ps1"', batch)
        self.assertIn('-Workspace "%ROOT%"', batch)

    @unittest.skipUnless(os.name == "nt", "Start-DailyExport.bat is Windows-only")
    def test_start_batch_passes_resolvable_workspace_without_trailing_quote(self) -> None:
        batch_path = Path("Start-DailyExport.bat").resolve()
        expected_workspace = batch_path.parent
        expected_script = expected_workspace / "scripts" / "run_daily_export.ps1"

        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp)
            capture_path = fake_bin / "captured-args.txt"
            # Intercept the unqualified `powershell` command so the real daily
            # export cannot start. `%~5` and `%~7` are the -File and -Workspace
            # values after cmd.exe has performed its actual quoting rules.
            (fake_bin / "powershell.cmd").write_text(
                "@echo off\r\n"
                "> \"%CAPTURE%\" echo SCRIPT=%~5\r\n"
                ">> \"%CAPTURE%\" echo WORKSPACE=%~7\r\n"
                "exit /b 0\r\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["CAPTURE"] = str(capture_path)
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")

            result = subprocess.run(
                ["cmd", "/d", "/c", str(batch_path)],
                cwd=expected_workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            captured = capture_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(captured, [f"SCRIPT={expected_script}", f"WORKSPACE={expected_workspace}"])

    def test_python_entrypoints_use_shared_workspace_discovery(self) -> None:
        for path in (
            Path("scripts/run_daily_export.py"),
            Path("scripts/process_existing_raw.py"),
            Path("scripts/archive_exports.py"),
            Path("scripts/export_on_demand.py"),
            Path("scripts/doctor.py"),
        ):
            script = path.read_text(encoding="utf-8")
            self.assertIn("resolve_config_path", script, path.as_posix())
            self.assertNotIn("Path.cwd() / config_path", script, path.as_posix())
            self.assertNotIn("ROOT / config_path", script, path.as_posix())

    def test_powershell_entrypoints_use_shared_workspace_discovery(self) -> None:
        for path in (
            Path("scripts/run_daily_export.ps1"),
            Path("scripts/process_existing_raw.ps1"),
            Path("scripts/Open-LatestInsights.ps1"),
            Path("scripts/Open-InsightsByDate.ps1"),
        ):
            script = path.read_text(encoding="utf-8-sig")
            self.assertIn("WorkspaceDiscovery.psm1", script, path.as_posix())
            self.assertIn("Resolve-WeChatDiaryWorkspace", script, path.as_posix())
            self.assertNotIn("$WorkspaceRoot = $CodeRoot", script, path.as_posix())
            self.assertNotIn("$Workspace = $CodeRoot", script, path.as_posix())

    def test_powershell_wrapper_does_not_treat_native_stderr_as_fatal(self) -> None:
        script = Path("scripts/run_daily_export.ps1").read_text(encoding="utf-8")

        self.assertIn("chcp 65001", script)
        self.assertIn("PYTHONIOENCODING", script)
        self.assertIn("cmd /d /c $CommandLine", script)
        self.assertNotIn("*>&1 | Tee-Object", script)

    def test_powershell_wrapper_filters_console_but_logs_everything(self) -> None:
        script = Path("scripts/run_daily_export.ps1").read_text(encoding="utf-8")

        self.assertIn("ShouldShowDailyExportLine", script)
        # every line still reaches the runlog file
        self.assertIn("Add-Content -LiteralPath $LogPath", script)
        # warnings, result summary and wizard prompts must never be filtered away
        self.assertIn("'^\\[WARN\\]'", script)
        self.assertIn("Archive root:", script)
        self.assertIn("self moments wxid:", script)
        # WeFlow per-session noise stays in the runlog, while empty sessions are aggregated.
        self.assertIn("$EmptySessionSkips", script)
        self.assertIn("^导出 .+ 失败:.*没有消息", script)
        self.assertIn("InitExportCursorHeap", script)
        self.assertIn("[stage] $EmptySessionSkips 个空会话跳过", script)
        # python runs unbuffered so stage progress streams live instead of dumping at exit
        self.assertIn('"python" -u', script)
        # partial failure is a distinct third state: "完成但有警告" + exit 1, not "failed"
        self.assertIn("CompletedWithWarnings", script)
        self.assertIn("完成但有警告", script)
        self.assertIn("Daily export completed( with warnings)?", script)
        self.assertIn("^manual backend:", script)
        self.assertIn("^config 建议迁移", script)

    def test_public_readme_has_no_model_signature_footer(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertNotIn("变更记录：2026-08-04", readme)
        self.assertNotRegex(readme, r"〔[^〕]+〕")

    def test_ensure_local_config_allows_empty_target_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            # Fixed API token, then empty answer to the self-moments question.
            answers = iter(["fixed-token", ""])

            ensure_local_config(
                config_path=config_path,
                example_path=Path("config.example.toml"),
                input_func=lambda _prompt: next(answers),
            )

            cfg = load_config(config_path)

        self.assertEqual(cfg.daily_export.target_usernames, [])
        self.assertEqual(cfg.daily_export.self_moments_usernames, [])
        # The empty answer is persisted as an explicit opt-out, not left dangling.
        self.assertTrue(cfg.daily_export.self_moments_configured)
        self.assertEqual(cfg.user.voice_transcribe_usernames, [])
        self.assertIsNone(cfg.daily_export.voice_fallback_script)
        self.assertEqual(cfg.daily_export.cleanup_mode, "archive")
        self.assertEqual(cfg.export_backend.backend, "weflow_api")
        self.assertEqual(cfg.export_backend.weflow_api.access_token, "fixed-token")

    def test_ensure_local_config_preserves_inline_comments_on_existing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            original = f"""
[user]
voice_transcribe_usernames = ["wxid_voice"]        # picked by hand, not the target

[automation]
weflow_exe = "{(root / 'WeFlow.exe').as_posix()}"

[daily_export]
target_usernames = ["wxid_existing"]               # keep these comments alive
target_processed_subroot = "_mine"                 # subroot doc
voice_fallback_script = ""                         # public default
cleanup_mode = "delete"                            # I really mean delete
restart_weflow = false                             # I manage WeFlow myself
""".strip()
            config_path.write_text(original, encoding="utf-8")

            ensure_local_config(
                config_path=config_path,
                example_path=Path("config.example.toml"),
                input_func=lambda _prompt: "",
            )

            updated = config_path.read_text(encoding="utf-8")
            self.assertIn('target_usernames = ["wxid_existing"]               # keep these comments alive', updated)
            self.assertIn('target_processed_subroot = "_mine"                 # subroot doc', updated)
            self.assertIn('self_moments_usernames = []', updated)

    def test_ensure_local_config_uses_target_as_voice_default_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            config_path.write_text(
                f"""
[user]
voice_transcribe_usernames = []

[automation]
weflow_exe = "{(root / 'WeFlow.exe').as_posix()}"

[daily_export]
target_usernames = ["wxid_target"]
""".strip(),
                encoding="utf-8",
            )

            ensure_local_config(
                config_path=config_path,
                example_path=Path("config.example.toml"),
                input_func=lambda _prompt: "wxid_me",
            )

            cfg = load_config(config_path)

        self.assertEqual(cfg.daily_export.target_usernames, ["wxid_target"])
        self.assertEqual(cfg.daily_export.self_moments_usernames, ["wxid_me"])
        self.assertTrue(cfg.daily_export.self_moments_configured)
        self.assertEqual(cfg.user.voice_transcribe_usernames, ["wxid_target"])

    def test_ensure_local_config_without_prompt_leaves_self_moments_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            config_path.write_text(
                f"""
[automation]
weflow_exe = "{(root / 'WeFlow.exe').as_posix()}"
""".strip(),
                encoding="utf-8",
            )

            ensure_local_config(
                config_path=config_path,
                example_path=Path("config.example.toml"),
                prompt=False,
            )

            updated = config_path.read_text(encoding="utf-8")
            cfg = load_config(config_path)

        # Non-interactive runs must not fabricate an opt-out the user never chose.
        self.assertNotIn("self_moments_usernames", updated)
        self.assertFalse(cfg.daily_export.self_moments_configured)

    def test_ensure_local_config_manual_backend_does_not_require_weflow_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            config_path.write_text(
                """
[export_backend]
backend = "manual"

[daily_export]
self_moments_usernames = []
""".strip(),
                encoding="utf-8",
            )

            ensure_local_config(
                config_path=config_path,
                example_path=Path("config.example.toml"),
                prompt=False,
                input_func=lambda _prompt: (_ for _ in ()).throw(AssertionError("must not prompt")),
            )
            cfg = load_config(config_path)

        self.assertEqual(cfg.export_backend.backend, "manual")

    def test_ensure_local_config_merges_legacy_weflow_path_with_new_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            config_path.write_text(
                f"""
[automation]
weflow_exe = "{(root / 'WeFlow.exe').as_posix()}"

[export_backend]
backend = "weflow"

[export_backend.weflow]
driver = "uia"
""".strip(),
                encoding="utf-8",
            )

            ensure_local_config(
                config_path=config_path,
                example_path=Path("config.example.toml"),
                prompt=False,
                input_func=lambda _prompt: (_ for _ in ()).throw(AssertionError("must not prompt")),
            )
            self.assertTrue(config_path.exists())

    def test_manual_backend_rejects_missing_raw_before_archiving_processed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, target_users="", backend="manual"))
            processed_file = cfg.paths.processed / "live.md"
            processed_file.parent.mkdir(parents=True)
            processed_file.write_text("live", encoding="utf-8")
            archive_calls: list[tuple] = []
            deps = DailyExportDeps(
                backend=FakeBackend(name="manual"),
                archive_existing_processed=lambda *args: archive_calls.append(args),
            )

            with self.assertRaises(DailyExportStageError) as captured:
                run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

            self.assertEqual(captured.exception.stage, "validate_raw_root")
            self.assertIn("Raw root does not exist:", str(captured.exception.cause))
            self.assertIn("canonical raw", str(captured.exception.cause))
            self.assertEqual(archive_calls, [])
            self.assertTrue(processed_file.exists())

    def test_manual_backend_rejects_empty_raw_before_archiving_processed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, target_users="", backend="manual"))
            cfg.paths.raw.mkdir(parents=True)
            processed_file = cfg.paths.processed / "live.md"
            processed_file.parent.mkdir(parents=True)
            processed_file.write_text("live", encoding="utf-8")
            archive_calls: list[tuple] = []
            deps = DailyExportDeps(
                backend=FakeBackend(name="manual"),
                archive_existing_processed=lambda *args: archive_calls.append(args),
            )

            with self.assertRaises(DailyExportStageError) as captured:
                run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

            self.assertEqual(captured.exception.stage, "validate_raw_root")
            self.assertIn("Raw root is empty:", str(captured.exception.cause))
            self.assertIn("canonical raw", str(captured.exception.cause))
            self.assertEqual(archive_calls, [])
            self.assertTrue(processed_file.exists())

    def test_runner_skips_target_steps_when_target_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, target_users=""))
            calls: list[tuple] = []
            deps = DailyExportDeps(
                backend=FakeBackend(calls=calls),
                rotate_export_workspace=lambda cfg, label, mode: calls.append(("rotate", label, mode))
                or SimpleNamespace(target=root / "rotation" / "run"),
                wait_for_raw_exports_stable=lambda raw_path, min_files: calls.append(("wait_raw", min_files)),
                run_voice_fallback_script=lambda script_path, config: calls.append(("fallback", script_path)),
                archive=lambda raw_path, config, clear_first: calls.append(("archive", clear_first)) or [root / "diary.md"],
                archive_chats_for=lambda usernames, config, subroot, image_mode, clear_first: calls.append(("sidecar_chats",)),
                archive_moments_for=lambda usernames, config, subroot, clear_first: calls.append(("sidecar_moments",)),
            )

            result = run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        self.assertEqual(result.day, "2026-05-16")
        self.assertEqual(
            [call[0] for call in calls],
            ["prepare_backend", "rotate", "all_chats", "wait_raw", "shutdown_backend", "archive"],
        )
        self.assertEqual(result.self_moment_files, [])
        self.assertEqual(result.sidecar_chat_files, [])
        self.assertEqual(result.sidecar_moment_files, [])

    def test_runner_reports_backend_prepare_failure_before_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, target_users=""))
            deps = _quiet_deps(root)
            deps.backend = FakeBackend(prepare_action=lambda: (_ for _ in ()).throw(RuntimeError("not ready")))

            with self.assertRaises(DailyExportStageError) as captured:
                run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        self.assertEqual(captured.exception.stage, "prepare_backend")
        self.assertIn("not ready", str(captured.exception.cause))

    def test_manual_backend_preserves_live_raw_and_runs_processed_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = _write_voice_failure_raw(root, message_ids=[72])
            previous_processed = root / "processed" / "Previous" / "2026-05-15.md"
            previous_processed.parent.mkdir(parents=True)
            previous_processed.write_text("previous", encoding="utf-8")
            cfg = load_config(_write_config(root, target_users="", backend="manual"))

            def forbidden(*args, **kwargs):
                raise AssertionError("manual mode must skip every online/rotation stage")

            backend = FakeBackend(
                name="manual",
                prepare_action=forbidden,
                export_chats_action=forbidden,
                export_moments_action=forbidden,
                transcribe_action=forbidden,
                shutdown_action=forbidden,
            )
            deps = DailyExportDeps(
                backend=backend,
                rotate_export_workspace=forbidden,
                wait_for_raw_exports_stable=forbidden,
                archive=archive,
                archive_chats_for=archive_chats_for,
                archive_moments_for=forbidden,
            )

            output = io.StringIO()
            with redirect_stdout(output):
                result = run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

            self.assertTrue(raw_path.exists())
            self.assertTrue((root / "archived" / "processed" / "Previous" / "2026-05-15.md").exists())
            self.assertEqual(backend.calls, [])
            self.assertIsNone(result.rotation_target)
            self.assertEqual(len(result.diary_files), 1)
            self.assertTrue(result.diary_files[0].exists())
            self.assertIn("prepare/rotate/export stages skipped", output.getvalue())

    def test_main_stops_weflow_after_stage_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root, target_users="")
            stop_calls: list[int] = []

            def fail_export(_cfg) -> None:
                raise DailyExportStageError("export_all_chats", RuntimeError("boom"))

            buffer = io.StringIO()
            with (
                patch("scripts.run_daily_export.run_daily_export", side_effect=fail_export),
                patch(
                    "scripts.run_daily_export.stop_weflow_processes",
                    side_effect=lambda timeout: stop_calls.append(int(timeout)) or True,
                ),
                redirect_stderr(buffer),
            ):
                exit_code = main(["--config", str(config_path), "--no-config-prompt"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stop_calls, [90])
        self.assertIn("FAILED at stage: export_all_chats", buffer.getvalue())

    def test_main_does_not_stop_weflow_after_manual_pipeline_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root, target_users="", backend="manual")
            failure = DailyExportStageError("archive_diary_processed", RuntimeError("bad raw"))
            with (
                patch("scripts.run_daily_export.run_daily_export", side_effect=failure),
                patch("scripts.run_daily_export.stop_weflow_processes") as stop,
                redirect_stderr(io.StringIO()),
            ):
                exit_code = main(["--config", str(config_path), "--no-config-prompt"])

        self.assertEqual(exit_code, 1)
        stop.assert_not_called()

    def test_main_does_not_stop_user_owned_weflow_after_api_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root, target_users="", backend="weflow_api")
            with config_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    '\n[export_backend.weflow_api]\nbase_url = "http://127.0.0.1:5031"\n'
                    'access_token = "fixed-token"\n'
                )
            failure = DailyExportStageError("export_all_chats", RuntimeError("api unavailable"))
            with (
                patch("scripts.run_daily_export.run_daily_export", side_effect=failure),
                patch("scripts.run_daily_export.stop_weflow_processes") as stop,
                redirect_stderr(io.StringIO()),
            ):
                exit_code = main(["--config", str(config_path), "--no-config-prompt"])

        self.assertEqual(exit_code, 1)
        stop.assert_not_called()

    def test_main_returns_1_with_warnings_on_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root, target_users="")
            partial_result = DailyExportResult(
                day="2026-05-16",
                rotation_target=None,
                diary_files=[root / "diary.md"],
                self_moment_files=[],
                sidecar_chat_files=[],
                sidecar_moment_files=[],
                partial_failures=["export_self_moments"],
            )

            out, err = io.StringIO(), io.StringIO()
            with (
                patch("scripts.run_daily_export.run_daily_export", return_value=partial_result),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                exit_code = main(["--config", str(config_path), "--no-config-prompt"])

        # partial = third state: non-zero exit + a distinct "with warnings" line.
        self.assertEqual(exit_code, 1)
        self.assertIn("Daily export completed with warnings.", out.getvalue())
        self.assertIn("export_self_moments", err.getvalue())
        self.assertIn("聊天或朋友圈", err.getvalue())
        self.assertNotIn("这些朋友圈", err.getvalue())
        self.assertTrue(all(len(line) < 240 for line in err.getvalue().splitlines()))

    def test_main_returns_zero_when_ignored_failures_create_no_partial_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root, target_users="")
            clean_result = DailyExportResult(
                day="2026-05-16",
                rotation_target=None,
                diary_files=[],
                self_moment_files=[],
                sidecar_chat_files=[],
                sidecar_moment_files=[],
                partial_failures=[],
            )

            with (
                patch("scripts.run_daily_export.run_daily_export", return_value=clean_result),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = main(["--config", str(config_path), "--no-config-prompt"])

        self.assertEqual(exit_code, 0)

    def test_runner_uses_target_usernames_when_voice_config_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root))
            calls: list[tuple] = []

            deps = DailyExportDeps(
                backend=FakeBackend(calls=calls),
                rotate_export_workspace=lambda cfg, label, mode: calls.append(("rotate", label, mode))
                or SimpleNamespace(target=root / "rotation" / "run"),
                wait_for_raw_exports_stable=lambda raw_path, min_files: calls.append(("wait_raw", min_files)),
                run_voice_fallback_script=lambda script_path, config: calls.append(("fallback", script_path)),
                archive=lambda raw_path, config, clear_first: calls.append(("archive", clear_first)) or [root / "diary.md"],
                archive_chats_for=lambda usernames, config, subroot, image_mode, clear_first: calls.append(
                    ("sidecar_chats", tuple(usernames), subroot, image_mode, clear_first)
                )
                or [root / "chats.md"],
                archive_moments_for=lambda usernames, config, subroot, clear_first: calls.append(
                    ("sidecar_moments", tuple(usernames), subroot, clear_first)
                )
                or [root / "moments.md"],
            )

            result = run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        self.assertEqual(result.day, "2026-05-16")
        self.assertEqual(
            [call[0] for call in calls],
            [
                "prepare_backend",
                "rotate",
                "voice",
                "all_chats",
                "wait_raw",
                "moments",
                "wait_raw",
                "shutdown_backend",
                "archive",
                "sidecar_chats",
                "sidecar_moments",
            ],
        )
        self.assertIn(("voice", ("Target",)), calls)
        self.assertIn(("all_chats", "2026-05-16"), calls)
        self.assertIn(("sidecar_chats", ("Target",), "_sidecar/chats", "preserve_paths", True), calls)
        self.assertIn(("sidecar_moments", ("Target",), "_sidecar/moments", True), calls)

    def test_runner_exports_and_archives_self_moments_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, target_users="", self_users='"Self"'))
            calls: list[tuple] = []

            deps = DailyExportDeps(
                backend=FakeBackend(calls=calls),
                rotate_export_workspace=lambda cfg, label, mode: SimpleNamespace(target=root / "rotation" / "run"),
                wait_for_raw_exports_stable=lambda raw_path, min_files: calls.append(("wait_raw", min_files)),
                run_voice_fallback_script=lambda script_path, config: calls.append(("fallback", script_path)),
                archive=lambda raw_path, config, clear_first: calls.append(("archive", clear_first)) or [root / "diary.md"],
                archive_chats_for=lambda usernames, config, subroot, image_mode, clear_first: [],
                archive_moments_for=lambda usernames, config, subroot, clear_first: calls.append(
                    ("archive_moments", tuple(usernames), subroot, clear_first)
                )
                or [root / "self-moments.md"],
            )

            result = run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        self.assertEqual(
            [call[0] for call in calls],
            [
                "prepare_backend",
                "all_chats",
                "wait_raw",
                "moments",
                "wait_raw",
                "shutdown_backend",
                "archive",
                "archive_moments",
            ],
        )
        self.assertIn(("moments", ("Self",)), calls)
        self.assertIn(("archive_moments", ("Self",), "朋友圈_自己", True), calls)
        self.assertEqual(result.self_moment_files, [root / "self-moments.md"])

    def test_self_moments_failure_does_not_abort_chat_diary(self) -> None:
        # A sidecar moments failure (e.g. WeFlow busy / CDP slow) must not throw
        # away the chat diary, which is the primary output. self fails, target +
        # diary still complete; the failure is recorded and warned, not raised.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, target_users='"Target"', self_users='"Self"'))
            calls: list[tuple] = []

            def export_moments(usernames, export_date):
                if tuple(usernames) == ("Self",):
                    raise RuntimeError("CDP busy: timed out")

            deps = DailyExportDeps(
                backend=FakeBackend(calls=calls, export_moments_action=export_moments),
                rotate_export_workspace=lambda cfg, label, mode: SimpleNamespace(target=root / "run"),
                wait_for_raw_exports_stable=lambda raw_path, min_files: None,
                run_voice_fallback_script=lambda script_path, config: None,
                archive=lambda raw_path, config, clear_first: calls.append(("archive",)) or [root / "diary.md"],
                archive_chats_for=lambda usernames, config, subroot, image_mode, clear_first: calls.append(
                    ("sidecar_chats",)
                )
                or [root / "chats.md"],
                archive_moments_for=lambda usernames, config, subroot, clear_first: calls.append(
                    ("archive_moments", tuple(usernames))
                )
                or [root / "m.md"],
            )

            buffer = io.StringIO()
            with redirect_stderr(buffer):
                result = run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))
            err = buffer.getvalue()

        # primary diary still produced
        self.assertIn(("archive",), calls)
        self.assertEqual(result.diary_files, [root / "diary.md"])
        # target moments succeeded -> archived; self failed -> archive skipped
        self.assertIn(("archive_moments", ("Target",)), calls)
        self.assertNotIn(("archive_moments", ("Self",)), calls)
        self.assertEqual(result.self_moment_files, [])
        # failure recorded + warned, not raised
        self.assertIn("export_self_moments", result.partial_failures)
        self.assertIn("[WARN]", err)

    def test_clean_run_has_no_partial_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, target_users="", self_users='"Self"'))
            deps = _quiet_deps(root)
            deps.archive = lambda raw_path, config, clear_first: [root / "diary.md"]
            deps.archive_moments_for = lambda usernames, config, subroot, clear_first: [root / "self.md"]

            result = run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        self.assertEqual(result.partial_failures, [])
        self.assertEqual(result.self_moment_files, [root / "self.md"])

    def test_shutdown_failure_warns_but_continues_archive_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, target_users=""))
            calls: list[tuple] = []

            def fail_shutdown() -> None:
                raise RuntimeError("cleanup unavailable")

            deps = DailyExportDeps(
                backend=FakeBackend(calls=calls, shutdown_action=fail_shutdown),
                rotate_export_workspace=lambda cfg, label, mode: SimpleNamespace(target=None),
                wait_for_raw_exports_stable=lambda raw_path, min_files: None,
                archive=lambda raw_path, config, clear_first: calls.append(("archive",)) or [],
                archive_chats_for=lambda usernames, config, subroot, image_mode, clear_first: [],
                archive_moments_for=lambda usernames, config, subroot, clear_first: [],
            )

            with redirect_stderr(io.StringIO()) as err:
                result = run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        self.assertIn(("archive",), calls)
        self.assertIn("shutdown_backend", result.partial_failures)
        self.assertIn("cleanup unavailable", err.getvalue())

    def test_runner_warns_loudly_when_self_moments_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, target_users="", self_users=None))
            deps = _quiet_deps(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                result = run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))
            output = buffer.getvalue()

        self.assertIn("Self moments contacts: NOT CONFIGURED", output)
        self.assertIn("[WARN]", output)
        self.assertIn("self_moments_usernames", output)
        self.assertEqual(result.self_moment_files, [])

    def test_runner_stays_quiet_when_self_moments_explicitly_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, target_users="", self_users=""))
            deps = _quiet_deps(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))
            output = buffer.getvalue()

        self.assertIn("explicitly disabled", output)
        self.assertNotIn("[WARN]", output)

    def test_runner_respects_explicit_voice_usernames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, voice_users='"VoiceOnly"'))
            calls: list[tuple] = []
            deps = DailyExportDeps(
                backend=FakeBackend(calls=calls),
                rotate_export_workspace=lambda cfg, label, mode: SimpleNamespace(target=None),
                wait_for_raw_exports_stable=lambda raw_path, min_files: None,
                archive=lambda raw_path, config, clear_first: [],
                archive_chats_for=lambda usernames, config, subroot, image_mode, clear_first: [],
                archive_moments_for=lambda usernames, config, subroot, clear_first: [],
            )

            run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        self.assertIn(("voice", ("VoiceOnly",)), calls)

    def test_runner_skips_unsupported_capabilities_but_keeps_chat_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(
                _write_config(root, target_users='"Target"', self_users='"Self"', voice_users='"Voice"')
            )
            calls: list[tuple] = []
            deps = DailyExportDeps(
                backend=FakeBackend(calls=calls, capabilities=frozenset()),
                rotate_export_workspace=lambda cfg, label, mode: SimpleNamespace(target=None),
                wait_for_raw_exports_stable=lambda raw_path, min_files: None,
                archive=lambda raw_path, config, clear_first: calls.append(("archive",)) or [],
                archive_chats_for=lambda usernames, config, subroot, image_mode, clear_first: calls.append(
                    ("sidecar_chats",)
                )
                or [],
                archive_moments_for=lambda usernames, config, subroot, clear_first: calls.append(
                    ("archive_moments",)
                )
                or [],
            )

            output = io.StringIO()
            with redirect_stdout(output):
                result = run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        call_names = [call[0] for call in calls]
        self.assertIn("all_chats", call_names)
        self.assertIn("archive", call_names)
        self.assertIn("sidecar_chats", call_names)
        self.assertNotIn("voice", call_names)
        self.assertNotIn("moments", call_names)
        self.assertNotIn("archive_moments", call_names)
        self.assertEqual(result.partial_failures, [])
        self.assertIn("voice_transcribe skipped: backend 'fake'", output.getvalue())
        self.assertIn("export_target_moments skipped: backend 'fake'", output.getvalue())
        self.assertIn("export_self_moments skipped: backend 'fake'", output.getvalue())

    def test_api_backend_skips_async_settle_and_propagates_session_partial_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, target_users="", backend="weflow_api"))
            backend = FakeBackend(name="weflow_api", capabilities=frozenset())
            backend.partial_failures = ["export_chat_session:wxid_placeholder"]
            deps = DailyExportDeps(
                backend=backend,
                rotate_export_workspace=lambda cfg, label, mode: SimpleNamespace(target=None),
                wait_for_raw_exports_stable=lambda *args: (_ for _ in ()).throw(
                    AssertionError("synchronous API backend must not poll raw stability")
                ),
                archive=lambda raw_path, config, clear_first: [],
                archive_chats_for=lambda *args, **kwargs: [],
                archive_moments_for=lambda *args, **kwargs: [],
            )

            output = io.StringIO()
            with redirect_stdout(output):
                result = run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        self.assertIn("export_chat_session:wxid_placeholder", result.partial_failures)
        self.assertIn("publishes validated session directories synchronously", output.getvalue())

    def test_api_backend_propagates_degraded_moments_media_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, target_users='"Target"', backend="weflow_api"))
            backend = FakeBackend(name="weflow_api", capabilities=frozenset({"moments"}))
            backend.partial_failures = []

            def export_moments(usernames, export_date):
                backend.partial_failures.append("export_moments_media:朋友圈导出_hash")

            backend.export_moments_action = export_moments
            deps = _quiet_deps(root)
            deps.backend = backend
            deps.archive = lambda raw_path, config, clear_first: []
            deps.archive_chats_for = lambda *args, **kwargs: []
            deps.archive_moments_for = lambda *args, **kwargs: []

            result = run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        self.assertIn("export_moments_media:朋友圈导出_hash", result.partial_failures)

    def test_runner_calls_voice_fallback_before_archiving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fallback = root / "voice_fallback.py"
            fallback.write_text("# placeholder", encoding="utf-8")
            cfg = load_config(_write_config(root, voice_fallback_script=fallback.as_posix()))
            calls: list[tuple] = []
            deps = DailyExportDeps(
                backend=FakeBackend(calls=calls),
                rotate_export_workspace=lambda cfg, label, mode: SimpleNamespace(target=None),
                wait_for_raw_exports_stable=lambda raw_path, min_files: calls.append(("wait_raw",)),
                run_voice_fallback_script=lambda script_path, config: calls.append(("fallback", Path(script_path).name)),
                archive=lambda raw_path, config, clear_first: calls.append(("archive",)) or [],
                archive_chats_for=lambda usernames, config, subroot, image_mode, clear_first: [],
                archive_moments_for=lambda usernames, config, subroot, clear_first: [],
            )

            run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        self.assertLess(calls.index(("fallback", "voice_fallback.py")), calls.index(("archive",)))

    def test_runner_reports_repeated_voice_failures_once_across_archive_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_voice_failure_raw(root)
            cfg = load_config(_write_config(root))
            deps = DailyExportDeps(
                backend=FakeBackend(),
                rotate_export_workspace=lambda cfg, label, mode: SimpleNamespace(target=None),
                wait_for_raw_exports_stable=lambda raw_path, min_files: None,
                archive=archive,
                archive_chats_for=archive_chats_for,
                archive_moments_for=lambda usernames, config, subroot, clear_first: [],
            )

            with self.assertLogs("wechat_diary_core.preprocessing.cleaner", level="WARNING") as captured:
                run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        log_text = "\n".join(record.getMessage() for record in captured.records)
        self.assertIn("[WARN] 3 条语音转写失败：message 72,73,74", log_text)
        self.assertEqual(log_text.count("Voice transcription failed in message 72."), 1)
        self.assertEqual(log_text.count("Voice transcription failed in message 73."), 1)
        self.assertEqual(log_text.count("Voice transcription failed in message 74."), 1)

    def test_runner_does_not_report_voice_failures_after_fallback_updates_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export_path = _write_voice_failure_raw(root, message_ids=[72])
            fallback = root / "voice_fallback.py"
            fallback.write_text("# placeholder", encoding="utf-8")
            cfg = load_config(_write_config(root, voice_fallback_script=fallback.as_posix()))

            def apply_fallback(script_path: str | Path, config) -> None:
                data = json.loads(export_path.read_text(encoding="utf-8"))
                data["messages"][0]["content"] = "[语音转文字] 已兜底"
                data["messages"][0]["voiceFallback"] = {"engine": "test"}
                export_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            deps = DailyExportDeps(
                backend=FakeBackend(),
                rotate_export_workspace=lambda cfg, label, mode: SimpleNamespace(target=None),
                wait_for_raw_exports_stable=lambda raw_path, min_files: None,
                run_voice_fallback_script=apply_fallback,
                archive=archive,
                archive_chats_for=archive_chats_for,
                archive_moments_for=lambda usernames, config, subroot, clear_first: [],
            )

            with self.assertNoLogs("wechat_diary_core.preprocessing.cleaner", level="WARNING"):
                run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

    def test_wait_for_raw_exports_stable_requires_a_written_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "raw"
            root.mkdir()

            with self.assertRaises(TimeoutError):
                wait_for_raw_exports_stable(root, quiet_seconds=0.01, timeout=0.03, poll_interval=0.01, min_files=1)

            (root / "export.json").write_text("{}", encoding="utf-8")

            snapshot = wait_for_raw_exports_stable(
                root,
                quiet_seconds=0.01,
                timeout=1,
                poll_interval=0.01,
                min_files=1,
            )

        self.assertEqual(snapshot.file_count, 1)
        self.assertGreater(snapshot.total_size, 0)


if __name__ == "__main__":
    unittest.main()
