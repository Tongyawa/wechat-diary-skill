from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
import stat

from wechat_diary_core.config import load_config
from wechat_diary_core.workspace import rotate_export_workspace


def _load_test_config(root: Path) -> object:
    config_path = root / "config.toml"
    config_path.write_text(
        f"""
[paths]
raw = "{(root / 'raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
rotation_root = "{(root / 'rotation').as_posix()}"

[automation]
weflow_exe = "{(root / 'WeFlow.exe').as_posix()}"
""".strip(),
        encoding="utf-8",
    )
    return load_config(config_path)


class WorkspaceRotationTests(unittest.TestCase):
    def test_moves_raw_and_processed_to_timestamped_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _load_test_config(root)
            (cfg.paths.raw / "session_a").mkdir(parents=True)
            (cfg.paths.raw / "session_a" / "data.json").write_text("{}", encoding="utf-8")
            (cfg.paths.processed / "session_a").mkdir(parents=True)
            (cfg.paths.processed / "session_a" / "2026-05-15.md").write_text("hello", encoding="utf-8")

            result = rotate_export_workspace(
                cfg,
                label="all_chats",
                timestamp=datetime(2026, 5, 16, 17, 30, 0),
            )

            self.assertIsNotNone(result.target)
            self.assertEqual(result.target.name, "20260516-173000-all_chats")
            self.assertTrue((result.moved["raw"] / "session_a" / "data.json").exists())
            self.assertTrue((result.moved["processed"] / "session_a" / "2026-05-15.md").exists())
            # raw / processed roots come back empty and ready for a fresh export
            self.assertEqual(list(cfg.paths.raw.iterdir()), [])
            self.assertEqual(list(cfg.paths.processed.iterdir()), [])

    def test_skips_rotation_when_workspace_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _load_test_config(root)
            cfg.paths.raw.mkdir(parents=True, exist_ok=True)
            cfg.paths.processed.mkdir(parents=True, exist_ok=True)

            result = rotate_export_workspace(cfg, label="all_chats")

            self.assertIsNone(result.target)
            self.assertEqual(result.moved, {})
            self.assertFalse(cfg.paths.rotation_root.exists() and any(cfg.paths.rotation_root.iterdir()))

    def test_only_rotates_the_populated_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _load_test_config(root)
            (cfg.paths.raw / "session_a").mkdir(parents=True)
            (cfg.paths.raw / "session_a" / "data.json").write_text("{}", encoding="utf-8")

            result = rotate_export_workspace(
                cfg,
                timestamp=datetime(2026, 5, 16, 17, 30, 0),
            )

            self.assertIsNotNone(result.target)
            self.assertIn("raw", result.moved)
            self.assertNotIn("processed", result.moved)
            self.assertFalse((result.target / "processed").exists())

    def test_delete_mode_wipes_without_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _load_test_config(root)
            (cfg.paths.raw / "session_a").mkdir(parents=True)
            (cfg.paths.raw / "session_a" / "data.json").write_text("{}", encoding="utf-8")
            (cfg.paths.processed / "session_a").mkdir(parents=True)
            (cfg.paths.processed / "session_a" / "2026-05-15.md").write_text("hi", encoding="utf-8")

            result = rotate_export_workspace(cfg, mode="delete")

            self.assertEqual(result.mode, "delete")
            self.assertIsNone(result.target)
            self.assertEqual(result.moved, {})
            self.assertEqual(list(cfg.paths.raw.iterdir()), [])
            self.assertEqual(list(cfg.paths.processed.iterdir()), [])
            # rotation_root should NOT be created when mode=delete
            self.assertFalse(cfg.paths.rotation_root.exists() and any(cfg.paths.rotation_root.iterdir()))

    def test_skip_mode_leaves_roots_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _load_test_config(root)
            (cfg.paths.raw / "session_a").mkdir(parents=True)
            (cfg.paths.raw / "session_a" / "data.json").write_text("{}", encoding="utf-8")

            result = rotate_export_workspace(cfg, mode="skip")

            self.assertEqual(result.mode, "skip")
            self.assertTrue((cfg.paths.raw / "session_a" / "data.json").exists())

    def test_archive_mode_handles_readonly_media_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _load_test_config(root)
            media = cfg.paths.raw / "session_a" / "media" / "videos" / "clip.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"video")
            media.chmod(stat.S_IREAD)

            result = rotate_export_workspace(
                cfg,
                label="all_chats",
                timestamp=datetime(2026, 5, 16, 17, 30, 0),
            )

            self.assertTrue((result.moved["raw"] / "session_a" / "media" / "videos" / "clip.mp4").exists())
            self.assertEqual(list(cfg.paths.raw.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
