"""Daily Moments use the existing post/media proof; bytes and reports are observable."""
from contextlib import nullcontext, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_moments_ranges as fixtures
from raw_isolation_support import assert_self_contained, isolated_file
from wechat_diary_core import raw_archive as raw, moments_ranges as ranges
from wechat_diary_core.chat_identity import _collision_failure_reason
from wechat_diary_core.backends.weflow_api import mapper


class DailyMomentsCoverageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MomentsRangeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.number = 0

    def scenario(self, failure=None):
        self.number += 1
        root = self.fixture.root / f"scenario-{self.number}"
        archive, live = root / "archived/raw", root / "live"
        day = date(2026, 1, 2)
        old_range, old_data = self.fixture.snapshot(archive, day=day,
            ids=("first", "gone") if failure == "post" else ("first",))
        new_range, new_data = self.fixture.snapshot(live, day=day,
            ids=("first", "new") if failure is None else ("first",))
        name = mapper.moments_filename([fixtures.SELF], day)
        old, incoming = archive / name, live / name
        old_range.rename(old)
        new_range.rename(incoming)
        for data in (old_data, new_data):
            for post in data["posts"]:
                post["media"] = []
        old_reference = self.fixture.media(archive)
        reference = self.fixture.media(live)
        old_data["posts"][0]["media"] = [{"localPath": old_reference}]
        new_data["posts"][0]["media"] = [{"localPath": reference}]
        new_data["exportTime"] = "2026-01-02T23:00:00"
        self.fixture.save(old, old_data)
        incoming.write_bytes(json.dumps(new_data, ensure_ascii=False, indent=2).replace("\n", "\r\n").encode("utf-8"))
        return archive, live, old, incoming, reference

    def check_outcome(self, failure=None):
        archive, live, old, incoming, reference = self.scenario(failure)
        before, expected = old.read_bytes(), incoming.read_bytes()
        fingerprint = ranges._fingerprint
        def unreadable(path):
            if Path(path) == live / reference:
                raise PermissionError("synthetic unreadable incoming object during proof")
            return fingerprint(path)
        with patch.object(ranges, "_fingerprint", side_effect=unreadable) if failure == "media" else nullcontext():
            plan = raw.plan_raw_ingest(archive, [live], policy="daily")
        with redirect_stdout(io.StringIO()):
            result = raw.apply_raw_ingest_plan(plan, move_source=True)
            raw.report_daily_result(plan, result)
        self.assertFalse(incoming.exists())
        if failure is None:
            self.assertEqual(old.read_bytes(), expected)
            self.assertFalse(result.retained)
            self.assertIsNone(result.backup_dir, "covered daily snapshots replace without an old-byte backup")
            self.assertFalse((archive.parent / "raw-folded").exists())
            self.assertFalse((archive.parent / "raw-fold-report.json").exists())
        else:
            self.assertEqual(old.read_bytes(), before)
            self.assertEqual(isolated_file(archive, live, incoming).read_bytes(), expected)
            assert_self_contained(result.isolation_dir)
            report = (archive.parent / "raw-fold-report.md").read_text(encoding="utf-8")
            self.assertIn("gone" if failure == "post" else reference, report)
            self.assertIn("缺少 post" if failure == "post" else "不可读", report)

    def test_superset_and_identical_media_replace_without_backup_or_report(self):
        self.check_outcome()

    def test_missing_post_preserves_old_and_reports_the_identity(self):
        self.check_outcome("post")

    def test_unreadable_media_proof_preserves_old_and_reports_the_reference(self):
        self.check_outcome("media")

    def test_constant_covered_predicate_is_rejected_for_both_regressions(self):
        for failure in ("post", "media"):
            with self.subTest(failure=failure):
                with patch.object(ranges, "compare_coverage", return_value=ranges.Coverage(())) as predicate:
                    with self.assertRaises(AssertionError):
                        self.check_outcome(failure)
                self.assertGreater(predicate.call_count, 0, "the mutation must actually reach the comparison")

    def test_daily_range_and_retention_names_share_proof_but_not_range_publication_scope(self):
        archive, live, old, incoming, _ = self.scenario()
        self.assertIsNone(ranges.range_digest(old))
        self.assertTrue(ranges.compare_coverage(old, incoming).covered)
        retained = old.with_name(old.stem + "_保留_" + "a" * 32 + ".json")
        old.rename(retained)
        self.assertTrue(ranges.compare_coverage(retained, incoming).covered)
        digest = incoming.stem.rsplit("_", 1)[1]
        ranged = incoming.with_name(f"朋友圈导出_区间_20260102-20260102_{digest}.json")
        incoming.rename(ranged)
        self.assertTrue(ranges.compare_coverage(retained, ranged).covered)
        self.assertIsNone(ranges.range_digest(retained))

    def test_different_digest_or_actual_account_set_never_proves_coverage(self):
        archive, live, old, incoming, _ = self.scenario()
        other = incoming.with_name(incoming.stem.rsplit("_", 1)[0] + "_deadbeef.json")
        incoming.rename(other)
        self.assertFalse(ranges.compare_coverage(old, other).covered)
        self.assertIsNotNone(_collision_failure_reason(other, old))
        other.rename(incoming)
        data = json.loads(old.read_bytes())
        data["filters"]["usernames"] = ["another_synthetic_account"]
        for post in data["posts"]:
            post["username"] = "another_synthetic_account"
        self.fixture.save(old, data)
        self.assertFalse(ranges.compare_coverage(old, incoming).covered)
        self.assertIsNotNone(_collision_failure_reason(incoming, old))

    def test_daily_media_roots_ignore_context_for_other_range_inputs(self):
        archive, live, old, incoming, reference = self.scenario("media")
        (live / reference).unlink()
        # An unrelated range in the same batch supplies prospective archive
        # media context. It cannot stand in for this daily input's own media.
        decision = _collision_failure_reason(incoming, old, range_media_root=archive,
                                             range_media_objects={})
        self.assertIsNotNone(decision)
        self.assertIn(reference, decision[0])
        self.fixture.media(live)
        self.assertIsNone(_collision_failure_reason(incoming, old, range_media_root=archive,
                                                    range_media_objects={}))
