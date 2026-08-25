from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest

from scripts.validate_weflow_automation import _changed_output_entries, _output_snapshot


class ValidateWeFlowAutomationScriptTests(unittest.TestCase):
    def test_media_store_is_not_reported_as_a_changed_session_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "_media" / "images" / "aa"
            media.mkdir(parents=True)
            (media / "aa000000000000000000000000000000.jpg").write_bytes(b"media")

            before = _output_snapshot(root)
            started_at = time.time()
            session = root / "私聊_测试"
            session.mkdir()
            (session / "2026-08-25.json").write_text("{}", encoding="utf-8")

            self.assertNotIn("_media", before)
            self.assertEqual(_changed_output_entries(root, before, started_at), ["私聊_测试"])

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
