from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from scripts.run_daily_export import DailyExportDeps, ensure_local_config, run_daily_export, wait_for_raw_exports_stable
from wechat_diary_core.config import load_config


def _write_config(root: Path, *, target_users: str = '"Target"', voice_users: str = "") -> Path:
    config_path = root / "config.toml"
    config_path.write_text(
        f"""
[user]
voice_transcribe_usernames = [{voice_users}]

[paths]
raw = "{(root / 'raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
rotation_root = "{(root / 'rotation').as_posix()}"

[automation]
weflow_exe = "{(root / 'WeFlow.exe').as_posix()}"

[daily_export]
target_usernames = [{target_users}]
target_processed_subroot = "_sidecar"
cleanup_mode = "archive"
restart_weflow = true
""".strip(),
        encoding="utf-8",
    )
    return config_path


class DailyExportScriptTests(unittest.TestCase):
    def test_powershell_wrapper_does_not_treat_native_stderr_as_fatal(self) -> None:
        script = Path("scripts/run_daily_export.ps1").read_text(encoding="utf-8")

        self.assertIn("chcp 65001", script)
        self.assertIn("PYTHONIOENCODING", script)
        self.assertIn("cmd /d /c $CommandLine", script)
        self.assertNotIn("*>&1 | Tee-Object", script)

    def test_ensure_local_config_allows_empty_target_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            answers = iter([(root / "WeFlow.exe").as_posix()])

            ensure_local_config(
                config_path=config_path,
                example_path=Path("config.example.toml"),
                input_func=lambda _prompt: next(answers),
            )

            cfg = load_config(config_path)

        self.assertEqual(cfg.daily_export.target_usernames, [])
        self.assertEqual(cfg.user.voice_transcribe_usernames, [])
        self.assertEqual(cfg.daily_export.cleanup_mode, "archive")

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
cleanup_mode = "delete"                            # I really mean delete
restart_weflow = false                             # I manage WeFlow myself
""".strip()
            config_path.write_text(original, encoding="utf-8")

            ensure_local_config(config_path=config_path, example_path=Path("config.example.toml"))

            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

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

            ensure_local_config(config_path=config_path, example_path=Path("config.example.toml"))

            cfg = load_config(config_path)

        self.assertEqual(cfg.daily_export.target_usernames, ["wxid_target"])
        self.assertEqual(cfg.user.voice_transcribe_usernames, ["wxid_target"])

    def test_runner_skips_target_steps_when_target_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, target_users=""))
            calls: list[tuple] = []
            deps = DailyExportDeps(
                stop_weflow_processes=lambda timeout: calls.append(("stop_weflow", int(timeout))),
                rotate_export_workspace=lambda cfg, label, mode: calls.append(("rotate", label, mode))
                or SimpleNamespace(target=root / "rotation" / "run"),
                ensure_weflow_running=lambda cfg: calls.append(("start_weflow",))
                or SimpleNamespace(cdp_endpoint="http://127.0.0.1:9222"),
                wait_for_ready_page=lambda endpoint: calls.append(("wait_ready", endpoint)),
                wait_for_raw_exports_stable=lambda raw_path, min_files: calls.append(("wait_raw", min_files)),
                batch_transcribe_voices_for=lambda usernames, config: calls.append(("voice", tuple(usernames))),
                export_all_chats=lambda date, config, cleanup: calls.append(("all_chats", cleanup)),
                export_moments_for=lambda usernames, date, config: calls.append(("moments", tuple(usernames))),
                archive=lambda raw_path, config, clear_first: calls.append(("archive", clear_first)) or [root / "diary.md"],
                archive_chats_for=lambda usernames, config, subroot, image_mode, clear_first: calls.append(("sidecar_chats",)),
                archive_moments_for=lambda usernames, config, subroot, clear_first: calls.append(("sidecar_moments",)),
            )

            result = run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        self.assertEqual(result.day, "2026-05-16")
        self.assertEqual(
            [call[0] for call in calls],
            ["stop_weflow", "rotate", "start_weflow", "wait_ready", "all_chats", "wait_raw", "archive"],
        )
        self.assertEqual(result.sidecar_chat_files, [])
        self.assertEqual(result.sidecar_moment_files, [])

    def test_runner_uses_target_usernames_when_voice_config_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root))
            calls: list[tuple] = []

            deps = DailyExportDeps(
                stop_weflow_processes=lambda timeout: calls.append(("stop_weflow", int(timeout))),
                rotate_export_workspace=lambda cfg, label, mode: calls.append(("rotate", label, mode))
                or SimpleNamespace(target=root / "rotation" / "run"),
                ensure_weflow_running=lambda cfg: calls.append(("start_weflow",))
                or SimpleNamespace(cdp_endpoint="http://127.0.0.1:9222"),
                wait_for_ready_page=lambda endpoint: calls.append(("wait_ready", endpoint)),
                wait_for_raw_exports_stable=lambda raw_path, min_files: calls.append(("wait_raw", min_files)),
                batch_transcribe_voices_for=lambda usernames, config: calls.append(("voice", tuple(usernames))),
                export_all_chats=lambda date, config, cleanup: calls.append(("all_chats", cleanup)),
                export_moments_for=lambda usernames, date, config: calls.append(("moments", tuple(usernames))),
                archive=lambda raw_path, config, clear_first: calls.append(("archive", clear_first)) or [root / "diary.md"],
                archive_chats_for=lambda usernames, config, subroot, image_mode, clear_first: calls.append(
                    ("sidecar_chats", tuple(usernames), subroot, image_mode, clear_first)
                )
                or [root / "chats.md"],
                archive_moments_for=lambda usernames, config, subroot, clear_first: calls.append(
                    ("sidecar_moments", usernames, subroot, clear_first)
                )
                or [root / "moments.md"],
            )

            result = run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        self.assertEqual(result.day, "2026-05-16")
        self.assertEqual(
            [call[0] for call in calls],
            [
                "stop_weflow",
                "rotate",
                "start_weflow",
                "wait_ready",
                "voice",
                "all_chats",
                "wait_raw",
                "moments",
                "wait_raw",
                "archive",
                "sidecar_chats",
                "sidecar_moments",
            ],
        )
        self.assertIn(("voice", ("Target",)), calls)
        self.assertIn(("all_chats", "skip"), calls)
        self.assertIn(("sidecar_chats", ("Target",), "_sidecar/chats", "preserve_paths", True), calls)
        self.assertIn(("sidecar_moments", None, "_sidecar/moments", True), calls)

    def test_runner_respects_explicit_voice_usernames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, voice_users='"VoiceOnly"'))
            calls: list[tuple] = []
            deps = DailyExportDeps(
                stop_weflow_processes=lambda timeout: None,
                rotate_export_workspace=lambda cfg, label, mode: SimpleNamespace(target=None),
                ensure_weflow_running=lambda cfg: SimpleNamespace(cdp_endpoint=None),
                wait_for_raw_exports_stable=lambda raw_path, min_files: None,
                batch_transcribe_voices_for=lambda usernames, config: calls.append(("voice", tuple(usernames))),
                export_all_chats=lambda date, config, cleanup: None,
                export_moments_for=lambda usernames, date, config: None,
                archive=lambda raw_path, config, clear_first: [],
                archive_chats_for=lambda usernames, config, subroot, image_mode, clear_first: [],
                archive_moments_for=lambda usernames, config, subroot, clear_first: [],
            )

            run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        self.assertEqual(calls, [("voice", ("VoiceOnly",))])

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
