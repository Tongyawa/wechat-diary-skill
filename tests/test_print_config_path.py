from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from scripts.print_config_path import main


class PrintConfigPathTests(unittest.TestCase):
    def test_prints_absolute_insights_path_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            insights = root / "external-insights"
            config = root / "config.toml"
            config.write_text(
                f'[paths]\ninsights = "{insights.as_posix()}"\n', encoding="utf-8"
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(["--config", str(config), "--key", "paths.insights"])

        self.assertEqual(result, 0)
        self.assertEqual(Path(output.getvalue().strip()), insights.resolve())

    def test_resolves_relative_insights_path_against_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text('[paths]\ninsights = "WeFlow-insights"\n', encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(["--config", str(config), "--key", "paths.insights"])

        self.assertEqual(result, 0)
        self.assertEqual(Path(output.getvalue().strip()), (root / "WeFlow-insights").resolve())

    def test_returns_error_without_config_or_with_invalid_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                missing_result = main(
                    ["--config", str(root / "missing.toml"), "--key", "paths.insights"]
                )

            invalid = root / "invalid.toml"
            invalid.write_text("[paths\n", encoding="utf-8")
            with contextlib.redirect_stderr(error):
                invalid_result = main(["--config", str(invalid), "--key", "paths.insights"])

        self.assertEqual(missing_result, 2)
        self.assertEqual(invalid_result, 2)
        self.assertIn("config.toml 不存在", error.getvalue())
        self.assertIn("读取 config.toml 失败", error.getvalue())


if __name__ == "__main__":
    unittest.main()
