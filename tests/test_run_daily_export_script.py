from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from scripts.run_daily_export import DailyExportDeps, ensure_local_config, run_daily_export
from wechat_diary_core.config import load_config


def _write_config(root: Path, *, voice_users: str = "") -> Path:
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
target_usernames = ["Target"]
target_processed_subroot = "_sidecar"
cleanup_mode = "archive"
restart_weflow = true
""".strip(),
        encoding="utf-8",
    )
    return config_path


class DailyExportScriptTests(unittest.TestCase):
    def test_ensure_local_config_creates_target_and_voice_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            answers = iter([(root / "WeFlow.exe").as_posix(), "wxid_target"])

            ensure_local_config(
                config_path=config_path,
                example_path=Path("config.example.toml"),
                input_func=lambda _prompt: next(answers),
            )

            cfg = load_config(config_path)

        self.assertEqual(cfg.daily_export.target_usernames, ["wxid_target"])
        self.assertEqual(cfg.user.voice_transcribe_usernames, ["wxid_target"])
        self.assertEqual(cfg.daily_export.cleanup_mode, "archive")

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
                "moments",
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
                batch_transcribe_voices_for=lambda usernames, config: calls.append(("voice", tuple(usernames))),
                export_all_chats=lambda date, config, cleanup: None,
                export_moments_for=lambda usernames, date, config: None,
                archive=lambda raw_path, config, clear_first: [],
                archive_chats_for=lambda usernames, config, subroot, image_mode, clear_first: [],
                archive_moments_for=lambda usernames, config, subroot, clear_first: [],
            )

            run_daily_export(cfg, deps=deps, day=date(2026, 5, 16))

        self.assertEqual(calls, [("voice", ("VoiceOnly",))])


if __name__ == "__main__":
    unittest.main()
