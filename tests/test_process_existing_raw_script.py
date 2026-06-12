from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from scripts.process_existing_raw import (
    ProcessExistingRawDeps,
    infer_single_day,
    process_existing_raw,
)
from wechat_diary_core.config import load_config


def _write_config(
    root: Path,
    *,
    target_users: str = "",
    self_users: str = "",
    voice_fallback_script: str = "",
) -> Path:
    config_path = root / "config.toml"
    config_path.write_text(
        f"""
[user]
voice_transcribe_usernames = []

[paths]
raw = "{(root / 'raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
rotation_root = "{(root / 'rotation').as_posix()}"

[automation]
weflow_exe = "{(root / 'WeFlow.exe').as_posix()}"

[daily_export]
target_usernames = [{target_users}]
self_moments_usernames = [{self_users}]
target_processed_subroot = "_sidecar"
voice_fallback_script = "{voice_fallback_script}"
cleanup_mode = "archive"
restart_weflow = true
""".strip(),
        encoding="utf-8",
    )
    return config_path


class ProcessExistingRawScriptTests(unittest.TestCase):
    def test_existing_processed_is_archived_without_moving_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            (raw / "私聊_A_20260609").mkdir()
            raw_marker = raw / "keep.txt"
            raw_marker.write_text("do not move", encoding="utf-8")
            processed_file = root / "processed" / "old" / "2026-06-08.md"
            processed_file.parent.mkdir(parents=True)
            processed_file.write_text("old", encoding="utf-8")
            cfg = load_config(_write_config(root))

            deps = ProcessExistingRawDeps(
                archive=lambda raw_path, config, clear_first: [config.paths.processed / "chat" / "2026-06-09.md"],
                archive_chats_for=lambda **kwargs: [],
                archive_moments_for=lambda *args, **kwargs: [],
                run_voice_fallback_script=lambda script_path, config, raw_root: None,
            )

            result = process_existing_raw(
                cfg,
                deps=deps,
                require_day=True,
                timestamp=datetime(2026, 6, 11, 12, 0, 0),
            )

            self.assertEqual(result.day, "2026-06-09")
            self.assertTrue(raw_marker.exists())
            self.assertEqual(result.processed_backup, root / "rotation" / "20260611-120000-process_existing_raw" / "processed")
            self.assertTrue((result.processed_backup / "old" / "2026-06-08.md").exists())

    def test_raw_root_is_passed_to_all_processing_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root, target_users='"Target"', self_users='"Self"'))
            raw = root / "manual_raw"
            (raw / "私聊_A_20260609").mkdir(parents=True)
            calls: list[tuple] = []

            deps = ProcessExistingRawDeps(
                archive=lambda raw_path, config, clear_first: calls.append(("archive", Path(raw_path), clear_first)) or [],
                archive_chats_for=lambda usernames, raw_path, config, subroot, image_mode, clear_first: calls.append(
                    ("chats", tuple(usernames), Path(raw_path), subroot, image_mode, clear_first)
                )
                or [],
                archive_moments_for=lambda usernames, raw_path, config, subroot, clear_first: calls.append(
                    ("moments", tuple(usernames), Path(raw_path), subroot, clear_first)
                )
                or [],
                run_voice_fallback_script=lambda script_path, config, raw_root: None,
            )

            result = process_existing_raw(cfg, raw_root=raw, require_day=True, deps=deps)

            self.assertEqual(result.day, "2026-06-09")
            self.assertIn(("archive", raw.resolve(), True), calls)
            self.assertIn(("moments", ("Self",), raw.resolve(), "朋友圈_自己", True), calls)
            self.assertIn(("chats", ("Target",), raw.resolve(), "_sidecar/chats", "preserve_paths", True), calls)
            self.assertIn(("moments", ("Target",), raw.resolve(), "_sidecar/moments", True), calls)
            self.assertTrue((root / "processed" / "_sidecar" / "chats" / "2026-06-09.md").exists())
            self.assertTrue((root / "processed" / "_sidecar" / "moments" / "2026-06-09.md").exists())

    def test_infer_single_day_from_raw_folder_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "私聊_A_20260609").mkdir()
            (root / "群聊_B_20260609").mkdir()

            self.assertEqual(infer_single_day(root), "2026-06-09")

    def test_require_day_fails_for_range_or_multi_day_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(_write_config(root))
            raw = root / "raw"
            (raw / "私聊_A_20250606-20260606").mkdir(parents=True)
            deps = ProcessExistingRawDeps(
                archive=lambda *args, **kwargs: [],
                archive_chats_for=lambda **kwargs: [],
                archive_moments_for=lambda *args, **kwargs: [],
                run_voice_fallback_script=lambda script_path, config, raw_root: None,
            )

            with self.assertRaisesRegex(ValueError, "Pass --day"):
                process_existing_raw(cfg, deps=deps, require_day=True)

    def test_voice_fallback_runs_before_archiving_with_selected_raw_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fallback = root / "voice_fallback.py"
            fallback.write_text("# placeholder", encoding="utf-8")
            cfg = load_config(_write_config(root, target_users='"Target"', voice_fallback_script=fallback.as_posix()))
            raw = root / "manual_raw"
            (raw / "私聊_A_20260609").mkdir(parents=True)
            calls: list[tuple] = []

            deps = ProcessExistingRawDeps(
                archive=lambda raw_path, config, clear_first: calls.append(("archive", Path(raw_path))) or [],
                archive_chats_for=lambda *args, **kwargs: [],
                archive_moments_for=lambda *args, **kwargs: [],
                run_voice_fallback_script=lambda script_path, config, raw_root: calls.append(
                    ("fallback", Path(script_path), Path(raw_root))
                ),
            )

            process_existing_raw(cfg, raw_root=raw, deps=deps)

        self.assertEqual(calls[0], ("fallback", fallback, raw.resolve()))
        self.assertEqual(calls[1], ("archive", raw.resolve()))


if __name__ == "__main__":
    unittest.main()
