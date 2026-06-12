from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from scripts.init_worktree_config import init_worktree_config


def _write_main_config(main_root: Path) -> Path:
    config = main_root / "config.toml"
    config.write_text(
        """
[paths]
raw = "WeFlow-raw-exports"
processed = "WeFlow-processed-exports"
insights = "WeFlow-insights"

[automation]
weflow_exe = "E:/Apps/WeFlow/WeFlow.exe"   # keep my comment

[daily_export]
target_usernames = ["wxid_t"]
voice_fallback_script = ".claude/skills/private/voice_fallback.py"
""".strip(),
        encoding="utf-8",
    )
    return config


class InitWorktreeConfigTests(unittest.TestCase):
    def test_writes_absolute_data_roots_anchored_at_main_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main_root = root / "main"
            worktree = root / "wt"
            main_root.mkdir()
            worktree.mkdir()
            _write_main_config(main_root)

            target = init_worktree_config(worktree, main_root)

            data = tomllib.loads(target.read_text(encoding="utf-8"))
            resolved_main = main_root.resolve().as_posix()
            for key in ("raw", "processed", "archived", "insights"):
                value = data["paths"][key]
                self.assertTrue(Path(value).is_absolute(), key)
                self.assertTrue(value.startswith(resolved_main), key)
            # private fallback script must also anchor at the main workspace
            self.assertEqual(
                data["daily_export"]["voice_fallback_script"],
                (main_root.resolve() / ".claude/skills/private/voice_fallback.py").as_posix(),
            )
            # untouched keys (and their comments) survive
            self.assertEqual(data["daily_export"]["target_usernames"], ["wxid_t"])
            self.assertIn("# keep my comment", target.read_text(encoding="utf-8"))

    def test_refuses_overwrite_without_force_and_refuses_main_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main_root = root / "main"
            worktree = root / "wt"
            main_root.mkdir()
            worktree.mkdir()
            _write_main_config(main_root)
            (worktree / "config.toml").write_text("x = 1", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "--force"):
                init_worktree_config(worktree, main_root)

            init_worktree_config(worktree, main_root, force=True)
            self.assertIn("[paths]", (worktree / "config.toml").read_text(encoding="utf-8"))

            with self.assertRaisesRegex(RuntimeError, "main workspace"):
                init_worktree_config(main_root, main_root)


if __name__ == "__main__":
    unittest.main()
