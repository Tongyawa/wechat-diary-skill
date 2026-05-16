from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wechat_diary_core.config import load_config
from wechat_diary_core.insights_io import flatten_messages, read_archived_exports


class InsightsIoTests(unittest.TestCase):
    def test_reads_markdown_chat_flow_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            config_path.write_text(
                f"""
[paths]
processed = "{(root / 'processed').as_posix()}"
insights = "{(root / 'insights').as_posix()}"
""".strip(),
                encoding="utf-8",
            )
            path = root / "processed" / "Chat" / "2026-05-15.md"
            path.parent.mkdir(parents=True)
            path.write_text("2026-05-15 18:13:23\n我：hello\n", encoding="utf-8")

            cfg = load_config(config_path)
            exports = read_archived_exports("2026-05-15", config=cfg)
            flattened = flatten_messages(exports)

        self.assertEqual(exports[0]["session"]["displayName"], "Chat")
        self.assertEqual(flattened[0]["type"], "chat_flow")
        self.assertIn("我：hello", flattened[0]["content"])


if __name__ == "__main__":
    unittest.main()
