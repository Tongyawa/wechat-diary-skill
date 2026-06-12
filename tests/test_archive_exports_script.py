from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.archive_exports import main as archive_exports_main


def _write_config(root: Path) -> Path:
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
    return config_path


class ArchiveExportsScriptTests(unittest.TestCase):
    def test_ingests_backfill_raw_and_processed_and_removes_emptied_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            raw_backfill = root / "WeFlow-raw-exports-去年"
            (raw_backfill / "私聊_A_20250606-20260606").mkdir(parents=True)
            (raw_backfill / "私聊_A_20250606-20260606" / "私聊_A_20250606-20260606.json").write_text(
                "{}", encoding="utf-8"
            )
            processed_backfill = root / "WeFlow-processed-exports-去年"
            (processed_backfill / "私聊_A").mkdir(parents=True)
            (processed_backfill / "私聊_A" / "2025-07-01.md").write_text("hi", encoding="utf-8")

            exit_code = archive_exports_main(
                [
                    "--config",
                    str(config_path),
                    "--raw-root",
                    str(raw_backfill),
                    "--processed-root",
                    str(processed_backfill),
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(
                (root / "archived" / "raw" / "私聊_A" / "私聊_A_20250606-20260606.json").exists()
            )
            self.assertTrue((root / "archived" / "processed" / "私聊_A" / "2025-07-01.md").exists())
            self.assertFalse(raw_backfill.exists())
            self.assertFalse(processed_backfill.exists())

    def test_keep_source_leaves_the_backfill_tree_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            processed_backfill = root / "snapshot-processed"
            (processed_backfill / "私聊_A").mkdir(parents=True)
            src = processed_backfill / "私聊_A" / "2025-07-01.md"
            src.write_text("hi", encoding="utf-8")

            exit_code = archive_exports_main(
                ["--config", str(config_path), "--processed-root", str(processed_backfill), "--keep-source"]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(src.exists())
            self.assertTrue((root / "archived" / "processed" / "私聊_A" / "2025-07-01.md").exists())


if __name__ == "__main__":
    unittest.main()
