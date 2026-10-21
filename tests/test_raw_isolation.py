"""Daily isolation invariants, failed writes and executable gate controls."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from test_raw_archive import payload, save, tree
from wechat_diary_core import raw_archive as raw, raw_isolation as isolation
from wechat_diary_core.config import load_config
from wechat_diary_core.workspace import rotate_export_workspace
from scripts import archive_exports, doctor


from raw_isolation_support import isolated_file, assert_self_contained


class RawIsolationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="raw-isolation-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.archive, self.live = self.root / "archived/raw", self.root / "live"
        self.archive.mkdir(parents=True)
        self.live.mkdir()

    def blocked(self, media=True):
        old = save(self.archive / "Alpha/Alpha_20300102.json", payload(wxid="wxid_victim"))
        incoming = payload()
        if media:
            incoming["messages"][0].update(source="../_media/images/ab/" + "ab" * 16 + ".jpg")
        source = save(self.live / "Alpha_20300102/Alpha_20300102.json", incoming)
        if media:
            attachment = (source.parent / incoming["messages"][0]["source"]).resolve()
            attachment.parent.mkdir(parents=True, exist_ok=True)
            attachment.write_bytes(b"synthetic referenced media")
        return old, source

    def execute(self):
        plan = raw.plan_raw_ingest(self.archive, [self.live], policy="daily")
        before = plan.to_dict()
        with redirect_stdout(io.StringIO()):
            result = raw.apply_raw_ingest_plan(plan, move_source=True)
            raw.report_daily_result(plan, result)
        self.assertEqual(plan.to_dict(), before)
        return plan, result

    def test_isolated_bytes_media_manifest_and_report(self):
        old, source = self.blocked()
        inputs = {p: p.read_bytes() for p in self.live.rglob("*") if p.is_file()}
        old_bytes = old.read_bytes()
        plan, result = self.execute()
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertEqual(list(p for p in self.live.rglob("*") if p.is_file()), [])
        for path, value in inputs.items():
            self.assertEqual(isolated_file(self.archive, self.live, path).read_bytes(), value)
        assert_self_contained(result.isolation_dir)
        manifest = result.isolation_dir.with_name(result.isolation_dir.name + ".manifest.json")
        document = json.loads(manifest.read_bytes())
        self.assertEqual({r["source"] for r in document["files"]}, {str(p) for p in inputs})
        for row in document["files"]:
            self.assertEqual(raw._fingerprint(result.isolation_dir / row["path"]), row["sha256"])
            self.assertIn("rotatedAt", row)
        self.assertFalse((result.isolation_dir / "manifest.json").exists())
        ledger = json.loads((self.archive.parent / "raw-fold-report.json").read_bytes())
        detail = next(d for i in ledger["items"] for d in i.get("details", []))
        self.assertEqual(Path(detail["retainedSource"]).read_bytes(), inputs[source])
        md = (self.archive.parent / "raw-fold-report.md").read_text(encoding="utf-8")
        self.assertIn(str(result.isolation_dir), md)
        self.assertIn("archive_exports.py --raw-root", md)
        self.assertNotIn("每天轮转会再次遇到", md)

    def test_root_guard_rejects_archive_and_source_overlaps_before_any_write(self):
        for destination_kind in ("archive", "source", "source-parent"):
            for disabled in (False, True):
                with self.subTest(kind=destination_kind, disabled=disabled):
                    self.blocked()
                    save(self.live / "Ready_20300102/Ready_20300102.json", payload(wxid="wxid_ready"))
                    destination = {"archive": self.archive, "source": self.live,
                                   "source-parent": self.live.parent}[destination_kind]
                    before = tree(self.archive)
                    plan = raw.plan_raw_ingest(self.archive, [self.live], policy="daily")
                    with patch.object(isolation, "retention_root", return_value=destination), \
                         patch.object(isolation, "isolate_retained"), redirect_stdout(io.StringIO()):
                        if disabled:
                            with patch.object(isolation, "check_isolation_roots", return_value=destination):
                                raw.apply_raw_ingest_plan(plan)
                        else:
                            result = raw.apply_raw_ingest_plan(plan)
                            self.assertEqual(len(result.retained), len(plan.actions))
                    # Removing the write-side gate must make this observation red.
                    self.assertEqual(tree(self.archive) == before, not disabled)
                    shutil.rmtree(self.archive)
                    self.archive.mkdir()

    def test_noop_isolation_and_omitted_cas_are_detected(self):
        old, source = self.blocked()
        plan = raw.plan_raw_ingest(self.archive, [self.live], policy="daily")
        with patch.object(isolation, "isolate_retained"), redirect_stdout(io.StringIO()):
            raw.apply_raw_ingest_plan(plan, move_source=True)
        with self.assertRaises(AssertionError):
            self.assertFalse(source.exists(), "non-ready source remains live")
        _, result = self.execute()
        assert_self_contained(result.isolation_dir)
        media = next((result.isolation_dir / "_media").rglob("*.jpg"))
        media.unlink()
        with self.assertRaisesRegex(AssertionError, "missing isolated CAS"):
            assert_self_contained(result.isolation_dir)

    def test_copy_failure_preserves_source_and_dependent_chat(self):
        old, source = self.blocked()
        before = {p: p.read_bytes() for p in self.live.rglob("*") if p.is_file()}
        original = raw._atomic_bytes
        def fail_media(path, data):
            if "-retained" in str(path) and path.suffix == ".jpg":
                raise OSError("synthetic copy failure")
            original(path, data)
        with patch.object(raw, "_atomic_bytes", side_effect=fail_media):
            _, result = self.execute()
        for p, value in before.items():
            self.assertEqual(p.read_bytes(), value)
        self.assertFalse(result.isolated_sources)
        self.assertFalse(list(result.isolation_dir.rglob("*.json")))
        self.assertTrue(result.warnings)

    def test_nonchat_json_in_blocked_group_is_copied_before_chat_and_removed_after_it(self):
        _, source = self.blocked()
        opaque = source.parent / "Zed_20300103.json"
        opaque.write_bytes(b"{}")
        _, result = self.execute()
        self.assertFalse(source.exists() or opaque.exists())
        self.assertEqual(isolated_file(self.archive, self.live, opaque).read_bytes(), b"{}")
        assert_self_contained(result.isolation_dir)

    def test_generated_oracle_rejects_noop_mover_and_self_containment_rejects_missing_copy(self):
        from test_raw_archive_round5 import observe_execution
        self.blocked()
        with patch.object(isolation, "isolate_retained"):
            _, _, violations, _ = observe_execution(self.archive, self.live, "daily")
        self.assertTrue(any(v[0] == "blocked_source_still_live" for v in violations))
        original = isolation.isolate_retained
        def omit_cas(plan, result):
            original(plan, result)
            for path in (result.isolation_dir / "_media").rglob("*"):
                if path.is_file():
                    path.unlink()
        with patch.object(isolation, "isolate_retained", side_effect=omit_cas):
            _, result = self.execute()
        with self.assertRaisesRegex(AssertionError, "missing isolated CAS"):
            assert_self_contained(result.isolation_dir)

    def test_same_second_batches_get_distinct_directories_and_keep_both_report_items(self):
        from datetime import datetime
        frozen = datetime(2030, 1, 3, 1, 2, 3)
        with patch.object(isolation, "datetime") as clock:
            clock.now.return_value = frozen
            self.blocked()
            _, first = self.execute()
            self.blocked()
            _, second = self.execute()
        self.assertEqual(second.isolation_dir.name, first.isolation_dir.name + "-1")
        ledger = json.loads((self.archive.parent / "raw-fold-report.json").read_bytes())
        self.assertEqual({i["isolationDirectory"] for i in ledger["items"] if "isolationDirectory" in i},
                         {str(first.isolation_dir), str(second.isolation_dir)})

    def test_manifest_failure_and_unlink_failure_preserve_originals(self):
        for failure in ("manifest", "unlink"):
            with self.subTest(failure=failure):
                old, source = self.blocked()
                original = raw._atomic_bytes
                def fail_manifest(path, data):
                    if path.name.endswith(".manifest.json"):
                        raise OSError("manifest failure")
                    original(path, data)
                guard = patch.object(raw, "_atomic_bytes", side_effect=fail_manifest) if failure == "manifest" else \
                        patch.object(raw, "_unlink", side_effect=OSError("unlink failure"))
                with guard:
                    _, result = self.execute()
                self.assertTrue(source.exists())
                self.assertTrue(next((self.live / "_media").rglob("*.jpg")).exists())
                assert_self_contained(result.isolation_dir)

    def test_bulk_and_rename_do_not_create_isolation(self):
        source = save(self.live / "Alpha_20300102/Alpha_20300102.json", payload())
        plan = raw.plan_raw_ingest(self.archive, [self.live])
        raw.apply_raw_ingest_plan(plan, move_source=True)
        plan = raw.plan_raw_ingest(self.archive, [self.archive], policy="rename",
                                  rename_targets={"Alpha": "Beta"})
        raw.apply_raw_ingest_plan(plan, move_source=True)
        self.assertFalse(isolation.retention_root(self.archive.parent).exists())

    def test_isolated_directory_can_be_planned_without_schema_metadata(self):
        self.blocked()
        _, result = self.execute()
        empty_archive = self.root / "another-archive/raw"
        output = io.StringIO()
        with redirect_stdout(output):
            code = archive_exports.main(["--print-plan", "--archive-raw-root", str(empty_archive),
                                         "--raw-root", str(result.isolation_dir)])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["summary"]["schemaInvalid"], 0)
        assert_self_contained(result.isolation_dir)

    def test_empty_rotation_clears_deleted_batches_and_reports_orphan_manifest(self):
        self.blocked()
        _, result = self.execute()
        config_path = self.root / "config.toml"
        config_path.write_text('[paths]\nraw="live"\nprocessed="processed"\narchived="archived"\n', encoding="utf-8")
        cfg = load_config(config_path)
        ledger_path = self.archive.parent / "raw-fold-report.json"
        before = ledger_path.read_bytes()
        rotate_export_workspace(cfg)
        self.assertEqual(ledger_path.read_bytes(), before)
        shutil.rmtree(result.isolation_dir)
        rotate_export_workspace(cfg)
        self.assertEqual(json.loads(ledger_path.read_bytes())["items"], [])
        md = ledger_path.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("孤儿 manifest", md)

    def test_doctor_reports_derived_root_without_creating_it(self):
        config_path = self.root / "config.toml"
        config_path.write_text('[paths]\narchived="archived"\n', encoding="utf-8")
        result = doctor._check_retention_root(load_config(config_path))
        self.assertEqual(result.details["directories"], 0)
        self.assertFalse(Path(result.details["path"]).exists())
