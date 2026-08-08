from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wechat_diary_core.backup_state import (
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_NEVER_RAN,
    STATUS_OK,
    STATUS_STALE,
    STATUS_UNREADABLE,
    evaluate_backup_state,
)
from wechat_diary_core.config import BackupConfig, BackupRepo


# Shape copied from a real Invoke-BundleBackup.ps1 run, not hand-invented:
# PowerShell round-trip timestamps carry SEVEN fractional digits, and
# Set-Content -Encoding UTF8 writes a BOM. Both bit us for real.
REAL_TIMESTAMP = "2026-08-08T20:54:22.8305510+08:00"


def _write_state(path: Path, payload: dict, *, bom: bool = True) -> None:
    """Write like PowerShell does: UTF-8 **with** BOM by default."""
    encoding = "utf-8-sig" if bom else "utf-8"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding=encoding)


def _config(dest: Path, *, stale_warn_days: int = 3) -> BackupConfig:
    return BackupConfig(
        bundle_dest=dest,
        keep=5,
        stale_warn_days=stale_warn_days,
        repos=[BackupRepo(name="demo", path=dest / "repo")],
    )


def _ok_payload(finished: str = REAL_TIMESTAMP) -> dict:
    return {
        "startedAt": finished,
        "finishedAt": finished,
        "overall": "ok",
        "repos": [{"name": "demo", "path": "X:/demo", "result": "ok", "bytes": 1}],
    }


class BackupStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_disabled_when_not_configured(self) -> None:
        cfg = BackupConfig(bundle_dest=None, keep=5, stale_warn_days=3, repos=[])
        state = evaluate_backup_state(cfg)
        self.assertEqual(STATUS_DISABLED, state.status)
        # An unconfigured optional feature must never nag -- warning about it
        # would train users to ignore this channel.
        self.assertFalse(state.needs_attention)

    def test_disabled_when_dest_set_but_no_repos(self) -> None:
        cfg = BackupConfig(bundle_dest=self.dest, keep=5, stale_warn_days=3, repos=[])
        self.assertEqual(STATUS_DISABLED, evaluate_backup_state(cfg).status)

    def test_never_ran_when_state_file_missing(self) -> None:
        state = evaluate_backup_state(_config(self.dest))
        self.assertEqual(STATUS_NEVER_RAN, state.status)
        self.assertTrue(state.needs_attention)

    def test_reads_state_written_with_bom(self) -> None:
        """Regression: PowerShell writes a BOM; plain utf-8 read raises.

        Caught only by a real end-to-end run -- a Python-written fixture has no
        BOM, so a naive test would have passed while the check was broken
        forever in production.
        """
        finished = datetime.now(timezone.utc).isoformat()
        _write_state(self.dest / "last-run.json", _ok_payload(finished), bom=True)
        self.assertEqual(STATUS_OK, evaluate_backup_state(_config(self.dest)).status)

    def test_reads_state_written_without_bom(self) -> None:
        finished = datetime.now(timezone.utc).isoformat()
        _write_state(self.dest / "last-run.json", _ok_payload(finished), bom=False)
        self.assertEqual(STATUS_OK, evaluate_backup_state(_config(self.dest)).status)

    def test_parses_powershell_seven_digit_fraction(self) -> None:
        _write_state(self.dest / "last-run.json", _ok_payload(REAL_TIMESTAMP))
        now = datetime.fromisoformat("2026-08-08T20:54:22.830551+08:00")
        state = evaluate_backup_state(_config(self.dest), now=now)
        self.assertEqual(STATUS_OK, state.status)
        self.assertIsNotNone(state.age_days)

    def test_failed_run_is_reported_with_failing_repo_names(self) -> None:
        payload = _ok_payload(datetime.now(timezone.utc).isoformat())
        payload["overall"] = "failed"
        payload["repos"] = [
            {"name": "good", "path": "X:/good", "result": "ok"},
            {"name": "gone", "path": "X:/gone", "result": "failed", "error": "仓路径不存在"},
        ]
        _write_state(self.dest / "last-run.json", payload)

        state = evaluate_backup_state(_config(self.dest))
        self.assertEqual(STATUS_FAILED, state.status)
        self.assertTrue(state.needs_attention)
        self.assertIn("gone", state.message)
        self.assertNotIn("good", state.message)

    def test_stale_when_older_than_threshold(self) -> None:
        finished = datetime.now(timezone.utc) - timedelta(days=10)
        _write_state(self.dest / "last-run.json", _ok_payload(finished.isoformat()))

        state = evaluate_backup_state(_config(self.dest, stale_warn_days=3))
        self.assertEqual(STATUS_STALE, state.status)
        self.assertTrue(state.needs_attention)
        self.assertAlmostEqual(10.0, state.age_days or 0, places=1)

    def test_fresh_run_within_threshold_is_ok(self) -> None:
        finished = datetime.now(timezone.utc) - timedelta(days=1)
        _write_state(self.dest / "last-run.json", _ok_payload(finished.isoformat()))

        state = evaluate_backup_state(_config(self.dest, stale_warn_days=3))
        self.assertEqual(STATUS_OK, state.status)
        self.assertFalse(state.needs_attention)

    def test_unreadable_when_json_is_corrupt(self) -> None:
        (self.dest / "last-run.json").write_text("{not json", encoding="utf-8")
        state = evaluate_backup_state(_config(self.dest))
        self.assertEqual(STATUS_UNREADABLE, state.status)
        self.assertTrue(state.needs_attention)

    def test_every_attention_message_carries_a_rerun_command(self) -> None:
        """铁律 5：报错要能引导行动，不能只报告状态。"""
        skill_root = Path("X:/skill")
        cases = []

        cases.append(evaluate_backup_state(_config(self.dest), skill_root=skill_root))

        payload = _ok_payload(datetime.now(timezone.utc).isoformat())
        payload["overall"] = "failed"
        _write_state(self.dest / "last-run.json", payload)
        cases.append(evaluate_backup_state(_config(self.dest), skill_root=skill_root))

        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        _write_state(self.dest / "last-run.json", _ok_payload(old))
        cases.append(evaluate_backup_state(_config(self.dest), skill_root=skill_root))

        for state in cases:
            with self.subTest(status=state.status):
                self.assertTrue(state.needs_attention)
                self.assertIn("Invoke-BundleBackup.ps1", state.message)


class BackupConfigParsingTests(unittest.TestCase):
    """[backup] 的 TOML 解析——含默认值与相对路径锚定。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _load(self, body: str):
        from wechat_diary_core.config import load_config

        path = self.workspace / "config.toml"
        path.write_text(body, encoding="utf-8")
        return load_config(path).backup

    def test_absent_section_is_disabled(self) -> None:
        backup = self._load("[user]\nself_wxids = []\n")
        self.assertFalse(backup.enabled)
        self.assertIsNone(backup.bundle_dest)
        self.assertEqual([], backup.repos)

    def test_dest_without_repos_stays_disabled(self) -> None:
        backup = self._load('[backup]\nbundle_dest = "bundles"\n')
        self.assertFalse(backup.enabled)

    def test_name_defaults_to_directory_leaf(self) -> None:
        backup = self._load(
            '[backup]\nbundle_dest = "bundles"\n'
            'repos = [ { path = "some/nested/my-repo" } ]\n'
        )
        self.assertTrue(backup.enabled)
        self.assertEqual("my-repo", backup.repos[0].name)

    def test_relative_paths_anchor_to_workspace(self) -> None:
        backup = self._load(
            '[backup]\nbundle_dest = "bundles"\nrepos = [ { path = "repo" } ]\n'
        )
        self.assertEqual(self.workspace / "bundles", backup.bundle_dest)
        self.assertEqual(self.workspace / "repo", backup.repos[0].path)
        self.assertEqual(self.workspace / "bundles" / "last-run.json", backup.state_file)

    def test_entries_without_path_are_dropped(self) -> None:
        backup = self._load(
            '[backup]\nbundle_dest = "bundles"\n'
            'repos = [ { name = "orphan" }, { path = "real" } ]\n'
        )
        self.assertEqual(["real"], [repo.name for repo in backup.repos])

    def test_defaults_when_keys_omitted(self) -> None:
        backup = self._load(
            '[backup]\nbundle_dest = "bundles"\nrepos = [ { path = "repo" } ]\n'
        )
        self.assertEqual(5, backup.keep)
        self.assertEqual(3, backup.stale_warn_days)


if __name__ == "__main__":
    unittest.main()
