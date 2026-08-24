from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace

from scripts.run_daily_export import _warn_if_archive_session_names_split
from wechat_diary_core.session_rename_alarm import (
    inspect_archive_session_names,
    update_session_rename_alarm,
)


def _write_session(root: Path, directory: str, wxid: str) -> None:
    session_dir = root / directory
    session_dir.mkdir(parents=True)
    (session_dir / f"{directory}_20260516.json").write_text(
        json.dumps({"session": {"wxid": wxid}}, ensure_ascii=False),
        encoding="utf-8",
    )


class SessionRenameAlarmTests(unittest.TestCase):
    def test_detects_same_wxid_in_two_human_named_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive_raw = Path(tmp) / "archived" / "raw"
            _write_session(archive_raw, "群聊_Alpha", "wxid_group_alpha")
            _write_session(archive_raw, "群聊_Beta", "wxid_group_alpha")
            _write_session(archive_raw, "私聊_Gamma", "wxid_peer_gamma")

            conflicts = inspect_archive_session_names(archive_raw)

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].wxid, "wxid_group_alpha")
        self.assertEqual(conflicts[0].directories, ("群聊_Alpha", "群聊_Beta"))

    def test_related_directory_names_still_need_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive_raw = Path(tmp) / "archived" / "raw"
            _write_session(archive_raw, "私聊_Alpha", "wxid_peer_alpha")
            _write_session(archive_raw, "私聊_Alpha-copy", "wxid_peer_alpha")

            conflicts = inspect_archive_session_names(archive_raw)

        self.assertNotIn("kind", conflicts[0].to_dict())

    def test_module_has_no_manual_variant_classifier(self) -> None:
        module_path = Path(__file__).parents[1] / "wechat_diary_core" / "session_rename_alarm.py"

        self.assertNotIn("MANUAL_VARIANT", module_path.read_text(encoding="utf-8"))

    def test_state_reports_first_observation_then_stays_quiet_until_directory_set_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_raw = root / "archived" / "raw"
            state_path = root / ".session-rename-state.json"
            _write_session(archive_raw, "私聊_Alpha", "wxid_peer_alpha")
            _write_session(archive_raw, "私聊_Beta", "wxid_peer_alpha")

            first = update_session_rename_alarm(archive_raw, state_path)
            second = update_session_rename_alarm(archive_raw, state_path)
            _write_session(archive_raw, "私聊_Gamma", "wxid_peer_alpha")
            changed = update_session_rename_alarm(archive_raw, state_path)

            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(len(first.new_conflicts), 1)
        self.assertEqual(second.new_conflicts, ())
        self.assertEqual(second.stable_conflict_count, 1)
        self.assertEqual(len(changed.new_conflicts), 1)
        self.assertEqual(state["reported"]["wxid_peer_alpha"]["directories"], ["私聊_Alpha", "私聊_Beta", "私聊_Gamma"])

    def test_runner_warning_is_short_and_does_not_repeat_for_unchanged_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_raw = root / "archived" / "raw"
            _write_session(archive_raw, "群聊_Alpha", "wxid_group_alpha")
            _write_session(archive_raw, "群聊_Beta", "wxid_group_alpha")
            cfg = SimpleNamespace(base_dir=root, paths=SimpleNamespace(archived=root / "archived"))

            first, second = io.StringIO(), io.StringIO()
            with redirect_stderr(first):
                _warn_if_archive_session_names_split(cfg)
            with redirect_stderr(second):
                _warn_if_archive_session_names_split(cfg)

        self.assertIn("身份冲突新增 1 条", first.getvalue())
        self.assertIn("不影响本轮导出", first.getvalue())
        self.assertEqual(second.getvalue(), "")
        self.assertTrue(all(len(line) <= 240 for line in first.getvalue().splitlines()))


if __name__ == "__main__":
    unittest.main()
