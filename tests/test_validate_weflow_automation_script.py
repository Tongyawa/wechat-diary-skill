from __future__ import annotations

from pathlib import Path
import unittest


class ValidateWeFlowAutomationScriptTests(unittest.TestCase):
    def test_manual_checkpoint_is_recorded(self) -> None:
        script = Path("scripts/validate_weflow_automation.py").read_text(encoding="utf-8")

        self.assertIn("multiple folder picker windows", script)
        self.assertIn("moments-picker", script)
        self.assertIn("all-chats-export", script)
        self.assertIn("moments-export", script)
        self.assertIn("No new or touched top-level output entry", script)
        self.assertIn("New or touched top-level output entries: {len(changed)}", script)


if __name__ == "__main__":
    unittest.main()
