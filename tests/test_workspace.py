from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import stat

from wechat_diary_core.config import load_config
from wechat_diary_core.workspace import (
    merge_raw_exports_into_archive,
    merge_tree,
    rotate_export_workspace,
)


def _load_test_config(root: Path) -> object:
    config_path = root / "config.toml"
    config_path.write_text(
        f"""
[paths]
raw = "{(root / 'raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
archived = "{(root / 'archived').as_posix()}"

[automation]
weflow_exe = "{(root / 'WeFlow.exe').as_posix()}"
""".strip(),
        encoding="utf-8",
    )
    return load_config(config_path)


class WorkspaceArchiveMergeTests(unittest.TestCase):
    def test_archive_mode_merges_per_session_into_library(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _load_test_config(root)
            (cfg.paths.raw / "私聊_A_20260611").mkdir(parents=True)
            (cfg.paths.raw / "私聊_A_20260611" / "私聊_A_20260611.json").write_text("{}", encoding="utf-8")
            (cfg.paths.processed / "私聊_A").mkdir(parents=True)
            (cfg.paths.processed / "私聊_A" / "2026-06-11.md").write_text("hello", encoding="utf-8")

            result = rotate_export_workspace(cfg, label="all_chats")

            self.assertEqual(result.target, cfg.paths.archived)
            # raw session folder loses its date suffix in the library
            self.assertTrue(
                (cfg.paths.archived / "raw" / "私聊_A" / "私聊_A_20260611.json").exists()
            )
            self.assertTrue(
                (cfg.paths.archived / "processed" / "私聊_A" / "2026-06-11.md").exists()
            )
            # raw / processed roots come back empty and ready for a fresh export
            self.assertEqual(list(cfg.paths.raw.iterdir()), [])
            self.assertEqual(list(cfg.paths.processed.iterdir()), [])

    def test_archive_mode_deduplicates_and_newer_file_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _load_test_config(root)
            old = cfg.paths.archived / "processed" / "私聊_A" / "2026-06-11.md"
            old.parent.mkdir(parents=True)
            old.write_text("old render", encoding="utf-8")
            (cfg.paths.processed / "私聊_A").mkdir(parents=True)
            (cfg.paths.processed / "私聊_A" / "2026-06-11.md").write_text("new render", encoding="utf-8")

            rotate_export_workspace(cfg)

            self.assertEqual(old.read_text(encoding="utf-8"), "new render")
            # exactly one copy survives
            day_files = list((cfg.paths.archived / "processed" / "私聊_A").iterdir())
            self.assertEqual(len(day_files), 1)

    def test_archive_mode_merges_non_session_root_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _load_test_config(root)
            cfg.paths.raw.mkdir(parents=True)
            (cfg.paths.raw / "朋友圈导出_123.json").write_text("{}", encoding="utf-8")
            (cfg.paths.raw / "media").mkdir()
            (cfg.paths.raw / "media" / "pic_0.jpg").write_bytes(b"jpg")

            rotate_export_workspace(cfg)

            self.assertTrue((cfg.paths.archived / "raw" / "朋友圈导出_123.json").exists())
            self.assertTrue((cfg.paths.archived / "raw" / "media" / "pic_0.jpg").exists())

    def test_skips_archiving_when_workspace_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _load_test_config(root)
            cfg.paths.raw.mkdir(parents=True, exist_ok=True)
            cfg.paths.processed.mkdir(parents=True, exist_ok=True)

            result = rotate_export_workspace(cfg, label="all_chats")

            self.assertIsNone(result.target)
            self.assertEqual(result.moved, {})
            self.assertFalse(cfg.paths.archived.exists() and any(cfg.paths.archived.iterdir()))

    def test_only_archives_the_populated_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _load_test_config(root)
            (cfg.paths.raw / "私聊_A_20260611").mkdir(parents=True)
            (cfg.paths.raw / "私聊_A_20260611" / "data.json").write_text("{}", encoding="utf-8")

            result = rotate_export_workspace(cfg)

            self.assertIn("raw", result.moved)
            self.assertNotIn("processed", result.moved)
            self.assertFalse((cfg.paths.archived / "processed").exists())

    def test_delete_mode_wipes_without_archiving(self) -> None:
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
            self.assertFalse(cfg.paths.archived.exists() and any(cfg.paths.archived.iterdir()))

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
            media = cfg.paths.raw / "私聊_A_20260611" / "media" / "videos" / "clip.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"video")
            media.chmod(stat.S_IREAD)
            # an older, also read-only copy already sits in the library
            old = cfg.paths.archived / "raw" / "私聊_A" / "media" / "videos" / "clip.mp4"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"old video")
            old.chmod(stat.S_IREAD)

            rotate_export_workspace(cfg, label="all_chats")

            self.assertEqual(old.read_bytes(), b"video")
            self.assertEqual(list(cfg.paths.raw.iterdir()), [])


class MergeHelperTests(unittest.TestCase):
    def test_merge_raw_strips_range_suffixes_for_multi_day_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "WeFlow-raw-exports-去年"
            (source / "私聊_A_20250606-20260606").mkdir(parents=True)
            (source / "私聊_A_20250606-20260606" / "私聊_A_20250606-20260606.json").write_text(
                "{}", encoding="utf-8"
            )
            (source / "私聊_A_20260611").mkdir()
            (source / "私聊_A_20260611" / "私聊_A_20260611.json").write_text("{}", encoding="utf-8")
            target = root / "archived" / "raw"

            count = merge_raw_exports_into_archive(source, target)

            self.assertEqual(count, 2)
            # both the range export and the single-day export land in ONE session folder
            session = target / "私聊_A"
            self.assertTrue((session / "私聊_A_20250606-20260606.json").exists())
            self.assertTrue((session / "私聊_A_20260611.json").exists())

    def test_merge_tree_keep_source_copies_instead_of_moving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "processed"
            (source / "私聊_A").mkdir(parents=True)
            src_file = source / "私聊_A" / "2026-06-11.md"
            src_file.write_text("hello", encoding="utf-8")
            target = root / "archived" / "processed"

            count = merge_tree(source, target, move=False)

            self.assertEqual(count, 1)
            self.assertTrue(src_file.exists())
            self.assertTrue((target / "私聊_A" / "2026-06-11.md").exists())


if __name__ == "__main__":
    unittest.main()
