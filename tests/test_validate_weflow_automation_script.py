from __future__ import annotations

from pathlib import Path
import unittest


class ValidateWeFlowAutomationScriptTests(unittest.TestCase):
    def test_manual_checkpoint_is_recorded(self) -> None:
        script = Path("scripts/validate_weflow_automation.py").read_text(encoding="utf-8")

        self.assertIn("multiple folder picker windows", script)
        self.assertIn("moments-picker", script)


if __name__ == "__main__":
    unittest.main()
