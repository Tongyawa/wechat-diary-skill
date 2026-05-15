from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wechat_diary_core.config import load_config


class ConfigTests(unittest.TestCase):
    def test_load_config_merges_defaults_and_resolves_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[automation]
driver = "uia"

[paths]
raw = "raw"
""".strip(),
                encoding="utf-8",
            )

            cfg = load_config(config_path)

        self.assertEqual(cfg.automation.driver, "uia")
        self.assertTrue(str(cfg.paths.raw).endswith("raw"))
        self.assertEqual(cfg.paths.processed.name, "WeFlow-processed-exports")
        self.assertEqual(cfg.skills.daily, ["wechat-diary"])


if __name__ == "__main__":
    unittest.main()
