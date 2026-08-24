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
    SessionRenameMerge,
    SessionRenameReconciliation,
    inspect_archive_session_names,
    update_session_rename_alarm,
    write_session_rename_report,
)


def _write_session(root: Path, directory: str, wxid: str) -> None:
    session_dir = root / directory
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / f"{directory}_20260516.json").write_text(
        json.dumps({"session": {"wxid": wxid}}, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_current_session(root: Path, directory: str, wxid: str, display_name: str) -> None:
    session_dir = root / directory
    session_dir.mkdir(parents=True)
    (session_dir / f"{directory}.json").write_text(
        json.dumps(
            {
                "session": {
                    "wxid": wxid,
                    "displayName": display_name,
                    "type": "群聊",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_chat(root: Path, directory: str, filename: str, wxid: str, message_times: list[int]) -> Path:
    path = root / directory / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "weflow": {"source": "http_api"},
                "session": {"wxid": wxid, "displayName": "Synthetic", "type": "群聊"},
                "messages": [
                    {"localId": index, "createTime": create_time, "platformMessageId": f"p{index}"}
                    for index, create_time in enumerate(message_times, start=1)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


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

    def test_runner_merges_into_this_run_display_name_writes_report_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_raw = root / "archived" / "raw"
            _write_session(archive_raw, "群聊_Alpha", "wxid_group_alpha")
            _write_session(archive_raw, "群聊_Beta", "wxid_group_alpha")
            state_path = root / ".session-rename-state.json"
            update_session_rename_alarm(archive_raw, state_path)
            _write_current_session(root / "raw", "群聊_Current_20260516", "wxid_group_alpha", "Current")
            cfg = SimpleNamespace(
                base_dir=root,
                paths=SimpleNamespace(archived=root / "archived", raw=root / "raw"),
            )

            first, second = io.StringIO(), io.StringIO()
            opened: list[Path] = []
            with redirect_stderr(first):
                _warn_if_archive_session_names_split(cfg, report_opener=opened.append)
            first_report_bytes = (root / ".session-rename-report.md").read_bytes()
            with redirect_stderr(second):
                _warn_if_archive_session_names_split(cfg, report_opener=opened.append)

            report_path = root / ".session-rename-report.md"
            first_reported = "合并报告" in first.getvalue()
            opened_correctly = opened == [report_path]
            merged_alpha = (archive_raw / "群聊_Current" / "群聊_Alpha_20260516.json").is_file()
            merged_beta = (archive_raw / "群聊_Current" / "群聊_Beta_20260516.json").is_file()
            old_directories_removed = not (archive_raw / "群聊_Alpha").exists() and not (archive_raw / "群聊_Beta").exists()
            report_has_merge = "本轮合并" in report_path.read_text(encoding="utf-8")
            second_run_did_not_rewrite_report = report_path.read_bytes() == first_report_bytes
            state_cleared = json.loads(state_path.read_text(encoding="utf-8"))["reported"] == {}

        self.assertTrue(first_reported)
        self.assertTrue(opened_correctly)
        self.assertTrue(merged_alpha)
        self.assertTrue(merged_beta)
        self.assertTrue(old_directories_removed)
        self.assertTrue(report_has_merge)
        self.assertTrue(second_run_did_not_rewrite_report)
        self.assertTrue(state_cleared)
        self.assertEqual(second.getvalue(), "")
        self.assertTrue(all(len(line) <= 240 for line in first.getvalue().splitlines()))

    def test_runner_moves_a_single_previous_name_on_its_first_new_name_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_raw = root / "archived" / "raw"
            _write_session(archive_raw, "群聊_Previous", "wxid_group_alpha")
            _write_current_session(root / "raw", "群聊_Current_20260516", "wxid_group_alpha", "Current")
            cfg = SimpleNamespace(
                base_dir=root,
                paths=SimpleNamespace(archived=root / "archived", raw=root / "raw"),
            )

            output = io.StringIO()
            opened: list[Path] = []
            with redirect_stderr(output):
                _warn_if_archive_session_names_split(cfg, report_opener=opened.append)

            report_path = root / ".session-rename-report.md"
            moved_file_exists = (archive_raw / "群聊_Current" / "群聊_Previous_20260516.json").is_file()
            previous_directory_removed = not (archive_raw / "群聊_Previous").exists()
            report_text = report_path.read_text(encoding="utf-8")

        self.assertTrue(moved_file_exists)
        self.assertTrue(previous_directory_removed)
        self.assertEqual(opened, [report_path])
        self.assertIn("本轮合并", report_text)
        self.assertIn("合并报告", output.getvalue())

    def test_guard_rejection_is_reported_once_then_a_new_move_reports_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_raw = root / "archived" / "raw"
            _write_chat(archive_raw, "群聊_OldA", "shared.json", "wxid_group_alpha", [1])
            _write_session(archive_raw, "群聊_OldB", "wxid_group_alpha")
            _write_chat(archive_raw, "群聊_Current", "shared.json", "wxid_group_alpha", [1, 2])
            _write_current_session(root / "raw", "群聊_Current_20260516", "wxid_group_alpha", "Current")
            cfg = SimpleNamespace(
                base_dir=root,
                paths=SimpleNamespace(archived=root / "archived", raw=root / "raw"),
            )

            first, second, third, changed = io.StringIO(), io.StringIO(), io.StringIO(), io.StringIO()
            opened: list[Path] = []
            with redirect_stderr(first):
                _warn_if_archive_session_names_split(cfg, report_opener=opened.append)
            report_path = root / ".session-rename-report.md"
            first_report_bytes = report_path.read_bytes()
            with redirect_stderr(second):
                _warn_if_archive_session_names_split(cfg, report_opener=opened.append)
            with redirect_stderr(third):
                _warn_if_archive_session_names_split(cfg, report_opener=opened.append)
            second_and_third_did_not_rewrite_report = report_path.read_bytes() == first_report_bytes
            _write_session(archive_raw, "群聊_OldA", "wxid_group_alpha")
            with redirect_stderr(changed):
                _warn_if_archive_session_names_split(cfg, report_opener=opened.append)

            report_text = report_path.read_text(encoding="utf-8")
            rejected_file_kept = (archive_raw / "群聊_OldA" / "shared.json").is_file()
            safe_file_merged = (archive_raw / "群聊_Current" / "群聊_OldB_20260516.json").is_file()
            later_safe_file_merged = (archive_raw / "群聊_Current" / "群聊_OldA_20260516.json").is_file()

        self.assertTrue(rejected_file_kept)
        self.assertTrue(safe_file_merged)
        self.assertTrue(later_safe_file_merged)
        self.assertIn("护栏拒绝", report_text)
        self.assertIn("合并报告", first.getvalue())
        self.assertEqual(second.getvalue(), "")
        self.assertEqual(third.getvalue(), "")
        self.assertEqual(len(opened), 2)
        self.assertIn("合并报告", changed.getvalue())
        self.assertTrue(second_and_third_did_not_rewrite_report)

    def test_rejection_only_report_uses_guard_stay_not_merge_heading(self) -> None:
        reconciliation = SessionRenameReconciliation(
            merges=(
                SessionRenameMerge(
                    wxid="wxid_group_alpha",
                    target_directory="群聊_Current",
                    source_directories=("群聊_Previous",),
                    moved_files=0,
                    duplicate_files=0,
                    rejected_files=(("群聊_Previous/shared.json", "synthetic guard rejection"),),
                ),
            ),
            stayed=(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            report_path = write_session_rename_report(reconciliation, Path(tmp) / "report.md")
            report_text = report_path.read_text(encoding="utf-8")

        self.assertIn("## 护栏停留", report_text)
        self.assertNotIn("## 本轮合并", report_text)

    def test_report_opener_uses_start_process_notepad(self) -> None:
        runner = (Path(__file__).parents[1] / "scripts" / "run_daily_export.py").read_text(encoding="utf-8")

        self.assertIn("Start-Process -FilePath 'notepad.exe'", runner)


if __name__ == "__main__":
    unittest.main()
