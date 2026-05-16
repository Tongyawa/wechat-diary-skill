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
        self.assertIn("--restart-weflow", script)
        self.assertIn("_wait_for_ready_page", script)
        self.assertIn("No new or touched top-level output entry", script)
        self.assertIn("New or touched top-level output entries: {len(changed)}", script)
        self.assertIn("Media checkboxes checked", script)
        self.assertIn("Downloaded media file count", script)
        self.assertIn("voice-transcribe", script)
        self.assertIn("batch_transcribe_voices_for", script)


if __name__ == "__main__":
    unittest.main()
