from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SkillLayoutTests(unittest.TestCase):
    def test_skill_is_discoverable_at_repository_root(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("\nname: wechat-diary-skill\n", skill)
        self.assertIn("references/prompt-daily.md", skill)
        self.assertIn("references/prompt-summarize.md", skill)
        self.assertFalse((ROOT / ".claude" / "skills" / "wechat-diary").exists())

    def test_prompt_contracts_live_in_references(self) -> None:
        daily = (ROOT / "references" / "prompt-daily.md").read_text(encoding="utf-8")
        summarize = (ROOT / "references" / "prompt-summarize.md").read_text(
            encoding="utf-8"
        )

        for prompt_number in range(7):
            self.assertIn(f"Prompt {prompt_number}", daily)
        self.assertIn("# {folder} 总结", summarize)
        self.assertNotIn("### Summarize Prompt", daily)


if __name__ == "__main__":
    unittest.main()
