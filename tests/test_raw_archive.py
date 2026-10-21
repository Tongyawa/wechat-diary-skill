"""Synthetic content with field order/newlines sampled from three export generations."""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from scripts import archive_exports
from wechat_diary_core import raw_archive as ra
from wechat_diary_core.session_rename_alarm import reconcile_archive_session_names
from wechat_diary_core.workspace import rotate_export_workspace, merge_raw_exports_into_archive


def payload(ids=(1, 2), *, wxid="wxid_sample_chat", generation="cas", text="message"):
    # Field ordering follows the sampled canonical documents. Every value is synthetic.
    session = dict(wxid=wxid, username=wxid, displayName="Current", nickname="Current",
                   remark="", type="私聊", messageCount=len(ids))
    messages = []
    for number in ids:
        message = dict(localId=number, createTime=100 + number,
                       formattedTime="2030-01-02 10:00:00", type="文本消息",
                       content=f"{text}-{number}", source="", isSend=0,
                       senderUsername="wxid_sample_sender", senderDisplayName="Sender",
                       senderRemark="", senderNickname="Sender", platformMessageId=f"sample-{number}")
        if generation == "cas":
            message["senderGroupNickname"] = ""
        if generation == "legacy":
            message = dict(localId=number + 100, createTime=100 + number,
                           formattedTime=message["formattedTime"], type="文本消息", localType=1,
                           content=message["content"], isSend=0,
                           senderUsername=message["senderUsername"], senderDisplayName="Sender",
                           source="", senderAvatarKey="", platformMessageId=message["platformMessageId"])
        messages.append(message)
    if generation == "legacy":
        session = {key: session[key] for key in ("wxid", "nickname", "remark", "displayName", "type")}
        session.update(lastTimestamp=102, messageCount=len(ids), avatar="")
        return dict(weflow=dict(version="1.0.3", exportedAt=1000, generator="WeFlow"),
                    session=session, messages=messages, avatars={})
    metadata = dict(source="http_api", format="json")
    if generation == "cas":
        metadata["mediaScheme"] = "cas-v1"
    return dict(session=session, messages=messages, weflow=metadata)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if data.get("weflow", {}).get("source") == "http_api":
        text = text.replace("\n", "\r\n")
    path.write_bytes(text.encode("utf-8"))
    return path


def tree(root):
    return {p.relative_to(root).as_posix(): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob("*") if p.is_file()}


class RawArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "archived" / "raw"
        self.incoming = self.root / "incoming"
        self.archive.mkdir(parents=True)
        self.incoming.mkdir()
        self.config = self.root / "config.toml"
        self.config.write_text('[paths]\nraw="live"\nprocessed="processed"\narchived="archived"\n', encoding="utf-8")

    def old(self, directory="私聊_Current", filename="私聊_Former_20300102.json", **kwargs):
        return save(self.archive / directory / filename, payload(**kwargs))

    def source(self, directory="私聊_Current_20300102", filename="私聊_Current_20300102.json", **kwargs):
        return save(self.incoming / directory / filename, payload(**kwargs))

    def plan(self, **kwargs):
        return ra.plan_raw_ingest(self.archive, [self.incoming], **kwargs)

    def cli(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = archive_exports.main(["--config", str(self.config), "--raw-root", str(self.incoming), *args])
        return code, output.getvalue(), error.getvalue()

    def rename(self, target="Current"):
        current = self.root / "current"
        save(current / f"私聊_{target}_20300103" / f"私聊_{target}_20300103.json", payload())
        return reconcile_archive_session_names(self.archive, current,
                    find_guard_failures=archive_exports._find_guard_failures,
                    files_equal=archive_exports._files_equal)

    def test_existing_day_fold_is_byte_exact_and_backup_is_recoverable(self):
        old = self.old()
        before = old.read_bytes()
        source = self.source()
        plan = self.plan()
        self.assertEqual(plan.actions[0].resolution, "existing_day")
        result = ra.apply_raw_ingest_plan(plan)
        target = self.archive / "私聊_Current" / source.name
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertFalse(old.exists())
        self.assertEqual((result.backup_dir / old.relative_to(self.archive)).read_bytes(), before)
        manifest = json.loads((result.backup_dir / "manifest.json").read_bytes())
        self.assertTrue(manifest["items"][0]["coverage"]["covered"])
        self.assertEqual(manifest["items"][0]["status"], "published")
        self.assertIn(b"\r\n", target.read_bytes())
        self.assertFalse(target.read_bytes().endswith(b"\n"))

    def test_existing_wxid_without_day_and_new_wxid(self):
        self.old(filename="私聊_Current_20300101.json")
        self.source(directory="私聊_Renamed_20300102")
        action = self.plan().actions[0]
        self.assertEqual(action.resolution, "existing_session")
        self.assertEqual(action.target.parent.name, "私聊_Current")
        fresh = self.source(directory="私聊_Fresh_20300103", filename="私聊_Fresh_20300103.json", wxid="wxid_new_sample")
        action = next(a for a in self.plan().actions if a.source == fresh)
        self.assertEqual(action.resolution, "new_session")
        self.assertEqual(action.target.parent.name, "私聊_Fresh")

    def test_same_directory_fold_does_not_warn_about_its_active_target_directory(self):
        self.old()
        self.source()
        plan = self.plan()
        result = ra.apply_raw_ingest_plan(plan)
        self.assertFalse(result.warnings)
        ra.report_daily_result(plan, result)
        self.assertFalse((self.archive.parent / "raw-fold-report.json").exists())

    def test_retained_old_media_is_in_readonly_plan_and_bulk_report(self):
        old = self.old(directory="私聊_Former")
        self.old(filename="Current_20300101.json")
        source = self.source()
        media = old.parent / "media" / "picture.jpg"
        media.parent.mkdir()
        media.write_bytes(b"legacy archive media")
        before = tree(self.root)
        code, output, error = self.cli("--print-plan")
        self.assertEqual(code, 0, error)
        expected = [{"directory": "私聊_Former", "reasons": ["legacy_media_retained"],
                     "remainingFiles": ["私聊_Former/media/picture.jpg"]}]
        self.assertEqual(json.loads(output)["retainedDirectories"], expected)
        self.assertEqual(tree(self.root), before)
        code, output, error = self.cli()
        self.assertEqual(code, 0, error)
        report = json.loads((self.archive.parent / "raw-fold-report.json").read_bytes())
        self.assertEqual(report["schemaVersion"], 2)
        self.assertEqual(report["items"], [dict(item, entrypoint="bulk") for item in expected])
        self.assertIn("归档待审查", output)
        self.assertTrue(media.exists())
        self.assertFalse(old.exists())
        self.assertEqual((self.archive / "私聊_Current" / source.name).read_bytes(), source.read_bytes())

    def test_empty_old_directory_is_not_predicted_as_retained_and_is_removed(self):
        old = self.old(directory="私聊_Former")
        self.old(filename="Current_20300101.json")
        self.source()
        plan = self.plan()
        self.assertEqual(plan.to_dict()["retainedDirectories"], [])
        result = ra.apply_raw_ingest_plan(plan)
        self.assertFalse(old.parent.exists())
        self.assertFalse(result.warnings)

    def test_same_batch_different_rename_files_report_one_pending_day(self):
        self.old(directory="私聊_FormerA", filename="FormerA_20300102.json")
        self.old(directory="私聊_FormerB", filename="FormerB_20300102.json", ids=(2,))
        result = self.rename()
        self.assertEqual(len(list((self.archive / "私聊_Current").glob("*.json"))), 2)
        self.assertEqual(result.merges[0].pending_days,
                         (("私聊_Current/2030-01-02", "同日多文件，待下次重写时收敛"),))

    def test_partial_rename_reports_retained_directory_without_repeat_alert(self):
        from wechat_diary_core.session_rename_alarm import update_session_rename_report_state, write_session_rename_report
        blocked = self.old(directory="私聊_Former", filename="same_20300102.json", ids=(2,))
        self.old(filename="same_20300102.json")
        self.old(directory="私聊_Former", filename="other_20300103.json")
        media = blocked.parent / "media" / "image.jpg"
        media.parent.mkdir()
        media.write_bytes(b"retained for blocked source")
        first = self.rename()
        self.assertEqual(first.merges[0].retained_directories,
                         (("私聊_Former", "legacy_media_retained; other_files_retained"),))
        report = self.root / "report.md"
        write_session_rename_report(first, report)
        self.assertIn("旧目录保留", report.read_text(encoding="utf-8"))
        state = self.root / "state.json"
        update_session_rename_report_state(first, state)
        second = self.rename()
        decision = update_session_rename_report_state(second, state)
        self.assertFalse(decision.report.has_reportable_items)

    def test_multiple_directories_requires_an_exact_name_match(self):
        self.old(directory="私聊_Current")
        self.old(directory="私聊_Former")
        self.source()
        plan = self.plan()
        self.assertEqual(plan.actions[0].target.parent.name, "私聊_Current")
        self.assertEqual(len(plan.actions[0].existing), 2)
        self.assertEqual(plan.summary["resolutionBlocked"], 0)
        self.incoming.rename(self.root / "previous")
        self.incoming.mkdir()
        self.source(directory="私聊_Unmatched_20300102")
        self.assertEqual(self.plan().summary["resolutionBlocked"], 1)

    def test_mixed_directory_is_hard_blocked_even_with_force(self):
        self.old()
        self.old(filename="Other_20300101.json", wxid="wxid_other_sample")
        self.source()
        before = tree(self.root)
        self.assertGreater(self.plan().summary["resolutionBlocked"], 0)
        code, _, _ = self.cli("--force-overwrite", "--move-source")
        self.assertEqual(code, 1)
        self.assertEqual(tree(self.root), before)

    def test_unknown_member_blocks_only_its_directory_and_undated_known_is_an_issue(self):
        self.old(filename="undated.json")
        self.source()
        plan = self.plan()
        self.assertEqual(plan.summary["resolutionBlocked"], 0)
        self.assertEqual(len(plan.index.issues), 1)
        self.assertEqual(plan.actions[0].existing, [])
        bad = self.archive / "私聊_Current" / "broken.json"
        bad.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.plan().summary["resolutionBlocked"], 1)
        bad.unlink()
        bad = self.archive / "Unrelated" / "broken.json"
        bad.parent.mkdir()
        bad.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.plan().summary["resolutionBlocked"], 0)

    def test_missing_identity_blocks_folding_and_force_cannot_bypass(self):
        old = self.old()
        source = self.source(ids=(2,))
        before = tree(self.root)
        plan = self.plan()
        self.assertEqual(plan.summary["coverageFailed"], 1)
        with self.assertRaises(ValueError):
            ra.apply_raw_ingest_plan(plan, force_overwrite=True)
        self.assertEqual(tree(self.root), before)
        self.assertTrue(old.exists() and source.exists())

    def test_three_generation_combinations_use_the_same_counter_proof(self):
        for generation, basis in [("legacy", "platformMessageId"), ("precas", "(createTime, localId)"),
                                  ("cas", "(createTime, localId)")]:
            with self.subTest(generation=generation):
                self.old(generation=generation)
                self.source()
                evidence = self.plan().actions[0].existing[0]["coverage"]
                self.assertTrue(evidence["covered"])
                self.assertEqual(evidence["basis"], basis)
                self.source(ids=(2,))
                self.assertFalse(self.plan().actions[0].existing[0]["coverage"]["covered"])

    def test_duplicate_platform_identity_requires_multiplicity(self):
        old = payload(generation="legacy")
        for message in old["messages"]:
            message["platformMessageId"] = "same-platform"
        save(self.archive / "私聊_Current" / "Former_20300102.json", old)
        new = payload(ids=(2,))
        new["messages"][0]["platformMessageId"] = "same-platform"
        save(self.incoming / "私聊_Current_20300102" / "Current_20300102.json", new)
        self.assertEqual(self.plan().summary["coverageFailed"], 1)

    def test_content_change_is_separate_from_coverage_and_requires_confirmation(self):
        self.old()
        self.source(text="changed")
        plan = self.plan()
        self.assertEqual(plan.summary["coverageFailed"], 0)
        self.assertEqual(plan.summary["forceEligible"], 1)
        with self.assertRaises(ValueError):
            ra.apply_raw_ingest_plan(plan)
        ra.apply_raw_ingest_plan(plan, force_overwrite=True)

    def test_unsupported_chat_shapes_block_bulk_but_daily_falls_back(self):
        for relative in ["私聊_Current_20300101-20300103/Current_20300101-20300103.json",
                         "私聊_Current/undated.json", "Root_20300102.json",
                         "私聊_Current/nested/Nested_20300102.json"]:
            with self.subTest(relative=relative):
                source = save(self.incoming / relative, payload())
                plan = self.plan(only_paths={source})
                self.assertEqual(plan.summary["resolutionBlocked"], 1)
                plan = self.plan(policy="daily", only_paths={source})
                self.assertEqual(plan.actions[0].resolution, "legacy_fallback")
                ra.apply_raw_ingest_plan(plan, move_source=True)
                self.assertFalse(source.exists())

    def test_daily_fallback_to_name_is_recorded_and_persistent_report_is_deduplicated(self):
        self.old(directory="私聊_Former")
        self.old(directory="私聊_Another")
        source = self.source()
        plan = self.plan(policy="daily")
        result = ra.apply_raw_ingest_plan(plan)
        self.assertEqual(plan.actions[0].resolution, "legacy_fallback")
        output = io.StringIO()
        with redirect_stdout(output):
            ra.report_daily_result(plan, result)
        report = self.archive.parent / "raw-fold-report.json"
        before = report.stat().st_mtime_ns
        repeated = io.StringIO()
        with redirect_stdout(repeated):
            ra.report_daily_result(plan, result)
        self.assertEqual(repeated.getvalue(), "")
        self.assertEqual(report.stat().st_mtime_ns, before)
        self.assertEqual((self.archive / "私聊_Current" / source.name).read_bytes(), source.read_bytes())

    def test_daily_same_path_subset_is_retained_by_final_inspection(self):
        old = self.old(filename="私聊_Current_20300102.json")
        before = old.read_bytes()
        source = self.source(ids=(2,))
        plan = self.plan(policy="daily")
        self.assertEqual(plan.summary["hardBlocked"], 1)
        ra.apply_raw_ingest_plan(plan)
        self.assertEqual(old.read_bytes(), before)
        self.assertTrue(source.exists())

    def test_daily_failed_fold_keeps_old_file_and_publishes_new(self):
        old = self.old()
        before = old.read_bytes()
        source = self.source(ids=(2,))
        result = ra.apply_raw_ingest_plan(self.plan(policy="daily"), move_source=True)
        self.assertEqual(old.read_bytes(), before)
        self.assertTrue(result.warnings)
        self.assertTrue((self.archive / "私聊_Current" / source.name).exists())

    def test_blocked_source_survives_rotation(self):
        first = self.source()
        second = self.source(directory="私聊_Another_20300102")
        cfg = SimpleNamespace(paths=SimpleNamespace(raw=self.incoming, processed=self.root / "processed",
                                                    archived=self.archive.parent))
        with redirect_stdout(io.StringIO()):
            rotate_export_workspace(cfg)
        self.assertTrue(first.exists() and second.exists())
        self.assertEqual(list(self.archive.rglob("*.json")), [])

    def test_media_follows_resolved_directory_and_conflicting_bytes_are_hard_blocked(self):
        self.old(filename="Current_20300101.json", generation="precas")
        source = self.source(directory="私聊_New_20300102", generation="precas")
        media = source.parent / "media" / "image.jpg"
        media.parent.mkdir()
        media.write_bytes(b"sample image")
        data = json.loads(source.read_bytes())
        data["messages"][0].update(type="图片消息", source="media/image.jpg", content="media/image.jpg")
        save(source, data)
        plan = self.plan()
        media_action = next(a for a in plan.actions if a.source == media)
        self.assertEqual(media_action.target, self.archive / "私聊_Current" / "media" / "image.jpg")
        ra.apply_raw_ingest_plan(plan)
        self.assertEqual(media_action.target.read_bytes(), media.read_bytes())
        media.write_bytes(b"different sample")
        plan = self.plan()
        self.assertGreater(plan.summary["hardBlocked"], 0)
        before = tree(self.root)
        with self.assertRaises(ValueError):
            ra.apply_raw_ingest_plan(plan, force_overwrite=True)
        self.assertEqual(tree(self.root), before)

    def test_rename_same_bytes_retires_source_only_after_backup(self):
        old = self.old(directory="私聊_Former")
        target = self.old(filename="Current_20300102.json")
        expected = old.read_bytes()
        result = self.rename()
        self.assertFalse(old.exists())
        self.assertEqual(target.read_bytes(), expected)
        self.assertEqual(result.merges[0].duplicate_files, 1)
        backups = list((self.archive.parent / "raw-folded").glob("*/私聊_Former/*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), expected)

    def test_rename_different_same_day_files_both_survive_with_original_basename(self):
        old = self.old(directory="私聊_Former", ids=(2,))
        target = self.old(filename="Current_20300102.json")
        before = old.read_bytes(), target.read_bytes()
        result = self.rename()
        self.assertFalse(old.exists())
        self.assertEqual((target.parent / old.name).read_bytes(), before[0])
        self.assertEqual(target.read_bytes(), before[1])
        self.assertTrue(result.merges[0].pending_days)

    def test_rename_pure_move_does_not_normalize_filename(self):
        old = self.old(directory="私聊_Former")
        with patch("wechat_diary_core.session_rename_alarm.apply_raw_ingest_plan", wraps=ra.apply_raw_ingest_plan) as execute:
            self.rename()
        self.assertEqual(execute.call_count, 1)
        self.assertTrue((self.archive / "私聊_Current" / old.name).exists())
        self.assertFalse((self.archive / "私聊_Current" / "私聊_Current_20300102.json").exists())

    def test_overlapping_roots_are_rejected_even_without_ranges(self):
        for source in [self.archive.parent, self.archive, self.root]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                ra.plan_raw_ingest(self.archive, [source])

    def test_duplicate_incoming_across_roots_is_blocked(self):
        self.source()
        other = self.root / "other"
        save(other / "私聊_Current_20300102" / "Current_20300102.json", payload())
        plan = ra.plan_raw_ingest(self.archive, [self.incoming, other])
        self.assertEqual(plan.summary["resolutionBlocked"], 2)

    def test_stale_plan_detects_source_changes_and_new_archive_sibling(self):
        source = self.source()
        plan = self.plan()
        source.write_bytes(source.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            ra.apply_raw_ingest_plan(plan)
        plan = self.plan()
        self.old()
        with self.assertRaises(ValueError):
            ra.apply_raw_ingest_plan(plan)

    def test_failure_before_publish_keeps_old_and_source(self):
        old, source = self.old(), self.source()
        plan = self.plan()
        real_write = ra._atomic_bytes
        def fail_target(path, data):
            if path == plan.actions[0].target:
                raise OSError("synthetic publication failure")
            real_write(path, data)
        with patch.object(ra, "_atomic_bytes", side_effect=fail_target), self.assertRaises(OSError):
            ra.apply_raw_ingest_plan(plan, move_source=True)
        self.assertTrue(old.exists() and source.exists())
        self.assertFalse(plan.actions[0].target.exists())

    def test_failure_after_publish_keeps_both_copies_and_backup(self):
        old, source = self.old(), self.source()
        plan = self.plan()
        with patch.object(ra, "_unlink", side_effect=OSError("synthetic retirement failure")), self.assertRaises(OSError):
            ra.apply_raw_ingest_plan(plan)
        self.assertEqual(plan.actions[0].target.read_bytes(), source.read_bytes())
        self.assertTrue(old.exists())
        self.assertTrue(list((self.archive.parent / "raw-folded").glob("*/私聊_Current/*.json")))

    def test_backup_reread_failure_cannot_retire_or_publish(self):
        old = self.old()
        before = old.read_bytes()
        source = self.source()
        plan = self.plan()
        real_atomic = ra._atomic_bytes
        corrupted = []
        def corrupt_landed_backup(path, data):
            real_atomic(path, data)
            if path.is_relative_to(self.archive.parent / 'raw-folded') and path.name != 'manifest.json':
                path.write_bytes(b'synthetic corrupted backup')
                corrupted.append(path)
        # Corrupt an actual landed file; a nonexistent allocation candidate
        # must retain the normal fingerprint contract of returning None.
        with patch.object(ra, "_atomic_bytes", side_effect=corrupt_landed_backup), self.assertRaises(OSError):
            ra.apply_raw_ingest_plan(plan)
        self.assertTrue(corrupted)
        self.assertTrue(all(p.read_bytes() == b'synthetic corrupted backup' for p in corrupted))
        self.assertEqual(old.read_bytes(), before)
        self.assertTrue(old.exists())
        self.assertTrue(source.exists())
        self.assertFalse(plan.actions[0].target.exists())

    def test_print_plan_is_zero_write_and_matches_execution(self):
        self.old()
        self.source()
        before = tree(self.root)
        with patch.object(Path, "mkdir", side_effect=AssertionError("read-only command tried mkdir")):
            code, output, _ = self.cli("--print-plan")
        self.assertEqual(code, 0)
        document = json.loads(output)
        self.assertEqual(tree(self.root), before)
        plan = self.plan()
        ra.apply_raw_ingest_plan(plan)
        for action in document["chatItems"]:
            target = self.archive / action["target"]
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), action["sourceSha256"])

    def test_print_plan_rejects_mutation_flags(self):
        self.source()
        for flag in ["--move-source", "--force-overwrite"]:
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                self.cli("--print-plan", flag)

    def test_hypothetical_override_reaches_plan_but_cannot_be_applied(self):
        self.old()
        source = self.source()
        before = tree(self.root)
        altered = json.dumps(payload(ids=(2,))).encode()
        plan = self.plan(incoming_overrides={source: altered})
        self.assertEqual(plan.summary["coverageFailed"], 1)
        self.assertEqual(tree(self.root), before)
        with self.assertRaises(ValueError):
            ra.apply_raw_ingest_plan(plan, force_overwrite=True)

    def test_common_executor_is_used_by_regular_schema_and_range_cli_paths(self):
        for mode in ["regular", "schema", "range"]:
            with self.subTest(mode=mode):
                # Keep other days untouched so each iteration proves its own route.
                day = {"regular": "20300102", "schema": "20300103", "range": "20300104"}[mode]
                self.source(directory=f"私聊_Current_{day}", filename=f"Current_{day}.json")
                bad = self.incoming / "bad.json"
                if mode == "schema":
                    bad.write_text("{}", encoding="utf-8")
                if mode == "range":
                    save(self.incoming / "朋友圈导出_区间_空_1234abcd.json",
                         {"filters": {"usernames": ["wxid_self_sample"]}, "posts": []})
                with patch.object(archive_exports, "apply_raw_ingest_plan", wraps=ra.apply_raw_ingest_plan) as execute:
                    code, _, error = self.cli()
                self.assertEqual(code, 1 if mode == "schema" else 0, error)
                self.assertEqual(execute.call_count, 1)
                self.assertTrue((self.archive / "私聊_Current" / f"私聊_Current_{day}.json").exists())
                bad.unlink(missing_ok=True)

    def test_daily_wrapper_uses_shared_executor(self):
        self.source()
        with patch("wechat_diary_core.workspace.apply_raw_ingest_plan", wraps=ra.apply_raw_ingest_plan) as execute:
            count = merge_raw_exports_into_archive(self.incoming, self.archive)
        self.assertEqual(count, 1)
        self.assertEqual(execute.call_count, 1)

    def test_legacy_acceptance_archive_cleanup_uses_shared_executor_without_gui(self):
        import sys
        from scripts import validate_weflow_automation as acceptance
        from wechat_diary_core.backends.weflow import exporter
        self.source()
        cfg = SimpleNamespace(paths=SimpleNamespace(raw=self.incoming, processed=self.root / "processed",
                                                    archived=self.archive.parent))
        with patch.object(acceptance, "load_config", return_value=cfg), \
             patch.object(acceptance, "_export_acceptance", side_effect=lambda cfg, label, action: (action(), 0)[1]), \
             patch.object(exporter, "_all_chats_commands", return_value=[]), \
             patch.object(exporter, "_run_export"), \
             patch.object(sys, "argv", ["validate_weflow_automation.py", "all-chats-export"]), \
             patch("wechat_diary_core.workspace.apply_raw_ingest_plan", wraps=ra.apply_raw_ingest_plan) as execute:
            self.assertEqual(acceptance.main(), 0)
        self.assertEqual(execute.call_count, 1)

    def test_daily_canonical_subset_is_not_exempt_when_a_sibling_exists(self):
        canonical = self.old(filename="私聊_Current_20300102.json")
        sibling = self.old(ids=(2,))
        source = self.source(ids=(2,))
        before = canonical.read_bytes()
        plan = self.plan(policy="daily")
        self.assertEqual(plan.summary["hardBlocked"], 1)
        ra.apply_raw_ingest_plan(plan)
        self.assertEqual(canonical.read_bytes(), before)
        self.assertTrue(sibling.exists())
        self.assertTrue(source.exists())

    def test_rename_preserves_shared_source_media_for_a_blocked_chat(self):
        blocked = self.old(directory="私聊_Former", filename="same_20300102.json", ids=(2,))
        self.old(filename="same_20300102.json")
        self.old(directory="私聊_Former", filename="other_20300103.json")
        media = blocked.parent / "media" / "image.jpg"
        media.parent.mkdir()
        media.write_bytes(b"still referenced by blocked chat")
        self.rename()
        self.assertTrue(blocked.exists())
        self.assertTrue(media.exists())
        self.assertEqual((self.archive / "私聊_Current" / "media" / "image.jpg").read_bytes(), media.read_bytes())
        self.assertTrue((self.archive / "私聊_Current" / "other_20300103.json").exists())

    def test_new_target_directory_cannot_acquire_two_incoming_wxids(self):
        self.source(directory="私聊_Current_20300102")
        self.source(directory="私聊_Current_20300103", filename="Current_20300103.json", wxid="wxid_other_sample")
        plan = self.plan()
        self.assertGreater(plan.summary["resolutionBlocked"], 0)
        with self.assertRaises(ValueError):
            ra.apply_raw_ingest_plan(plan)

    def test_rename_late_guard_keeps_shared_source_media(self):
        blocked = self.old(directory='私聊_Former', filename='blocked_20300102.json')
        good = self.old(directory='私聊_Former', filename='good_20300103.json')
        media = blocked.parent / 'media/pic.jpg'
        media.parent.mkdir()
        media.write_bytes(b'shared legacy dependency')
        plan = ra.plan_raw_ingest(self.archive, [self.archive], policy='rename',
            only_paths={blocked, good, media}, rename_targets={'私聊_Former': '私聊_Current'})
        # The rename caller can add a stricter guard after planning. It may only
        # narrow execution; source cleanup must honor the resulting retention.
        next(a for a in plan.actions if a.source == blocked).block('caller_guard', 'hardBlocked')
        result = ra.apply_raw_ingest_plan(plan, move_source=True)
        self.assertTrue(blocked.exists())
        self.assertTrue(media.exists(), 'late dependency retention must also suppress source retirement')
        self.assertIn(media, result.retained)
        self.assertNotIn(media, result.retired)
        self.assertEqual((self.archive / '私聊_Current/media/pic.jpg').read_bytes(), media.read_bytes())

    def test_competing_media_targets_block_dependent_chats_after_final_pairing(self):
        sources = []
        for day, content in [("20300102", b"first"), ("20300103", b"second")]:
            source = self.source(directory=f"私聊_Current_{day}", filename=f"Current_{day}.json", generation="precas")
            data = json.loads(source.read_bytes())
            data["messages"][0].update(type="图片消息", source="media/pic.jpg", content="media/pic.jpg")
            save(source, data)
            media = source.parent / "media" / "pic.jpg"
            media.parent.mkdir()
            media.write_bytes(content)
            sources.append(source)
        plan = self.plan(policy="daily")
        self.assertTrue(all(a.status != "ready" for a in plan.actions if a.kind == "chat"))
        result = ra.apply_raw_ingest_plan(plan, move_source=True)
        self.assertEqual(result.published, [])
        self.assertTrue(all(p.exists() for p in sources))

    def test_same_batch_identical_renames_create_a_retirement_backup(self):
        first = self.old(directory="私聊_FormerA", filename="common_20300102.json")
        second = self.old(directory="私聊_FormerB", filename="common_20300102.json")
        expected = first.read_bytes()
        result = self.rename()
        self.assertFalse(first.exists() or second.exists())
        self.assertEqual(sum(m.duplicate_files for m in result.merges), 1)
        self.assertEqual((self.archive / "私聊_Current" / first.name).read_bytes(), expected)
        self.assertEqual(len(list((self.archive.parent / "raw-folded").glob("*/私聊_Former*/*.json"))), 2)

    def test_same_batch_identical_renames_with_different_basenames_also_fold(self):
        self.old(directory="私聊_FormerA", filename="FormerA_20300102.json")
        self.old(directory="私聊_FormerB", filename="FormerB_20300102.json")
        result = self.rename()
        self.assertEqual(sum(m.duplicate_files for m in result.merges), 1)
        self.assertEqual(len(list((self.archive / "私聊_Current").glob("*.json"))), 1)

    def test_identical_rename_manifest_uses_byte_proof_when_identity_metadata_is_unavailable(self):
        for same_batch in (False, True):
            for empty in (False, True):
                with self.subTest(same_batch=same_batch, empty=empty), tempfile.TemporaryDirectory() as tmp:
                    archive = Path(tmp) / "archived" / "raw"
                    data = payload(ids=() if empty else (1, 2))
                    data.pop("weflow")
                    for message in data["messages"]:
                        message.pop("platformMessageId")
                    old = save(archive / "FormerB" / "FormerB_20300102.json", data)
                    peer = save(archive / ("FormerA" if same_batch else "Current") / "Peer_20300102.json", data)
                    selected = {old, peer} if same_batch else {old}
                    plan = ra.plan_raw_ingest(archive, [archive], policy="rename", only_paths=selected,
                               rename_targets={"FormerA": "Current", "FormerB": "Current"})
                    self.assertEqual(plan.summary["coverageFailed"], 0)
                    duplicate = next(a for a in plan.actions if a.duplicate)
                    record = duplicate.existing[0]
                    self.assertTrue(record["coverage"]["covered"])
                    self.assertEqual(record['coverage']['basis'], 'identical_sha256')
                    self.assertFalse(record["coverageRequired"])
                    self.assertEqual(record["retirementBasis"], "identical_sha256")
                    expected = old.read_bytes()
                    result = ra.apply_raw_ingest_plan(plan, move_source=True)
                    manifest = json.loads((result.backup_dir / "manifest.json").read_bytes())
                    self.assertEqual(manifest["items"][0]["retirementBasis"], "identical_sha256")
                    self.assertEqual(manifest["items"][0]["sha256"], manifest["items"][0]["incomingSha256"])
                    self.assertEqual(duplicate.target.read_bytes(), expected)
                    self.assertFalse(old.exists())

    def test_blocked_daily_chat_keeps_its_shared_input_cas(self):
        first = self.source()
        self.source(directory="私聊_Another_20300102")
        self.source(directory="私聊_Current_20300103", filename="Current_20300103.json")
        content = b"sample attachment"
        digest = hashlib.md5(content).hexdigest()
        media = self.incoming / "_media/images" / digest[:2] / f"{digest}.jpg"
        media.parent.mkdir(parents=True)
        media.write_bytes(content)
        data = json.loads(first.read_bytes())
        reference = "../" + media.relative_to(self.incoming).as_posix()
        data["messages"][0].update(type="图片消息", source=reference, content=reference)
        save(first, data)
        result = ra.apply_raw_ingest_plan(self.plan(policy="daily"), move_source=True)
        self.assertTrue(first.exists() and media.exists())
        self.assertIn(media, result.retained)
        self.assertEqual((self.archive / media.relative_to(self.incoming)).read_bytes(), content)

    def test_cas_target_conflict_blocks_chats_in_both_source_roots(self):
        self.source()
        second = self.root / "second"
        save(second / "私聊_Current_20300103" / "Current_20300103.json", payload())
        for root, data in [(self.incoming, b"first"), (second, b"second")]:
            media = root / "_media/images/aa" / ("a" * 32 + ".jpg")
            media.parent.mkdir(parents=True)
            media.write_bytes(data)
            # Both chats actually depend on the colliding object. Unrelated
            # chats in the same source root are not a publication dependency.
            chat_path, = root.glob('*/*.json')
            doc = json.loads(chat_path.read_bytes())
            doc['messages'][0]['source'] = '../_media/images/aa/' + 'a' * 32 + '.jpg'
            save(chat_path, doc)
        plan = ra.plan_raw_ingest(self.archive, [self.incoming, second], policy="daily")
        self.assertTrue(all(a.status != "ready" for a in plan.actions if a.kind == "chat"))
        self.assertEqual(ra.apply_raw_ingest_plan(plan).published, [])

    def test_preflight_headers_are_reused_without_reopening_unchanged_files(self):
        from wechat_diary_core.backends.weflow_api.backend import _stored_session_directory_collisions
        self.old()
        headers = {}
        self.assertEqual(_stored_session_directory_collisions(self.archive, header_cache=headers), {})
        with patch.object(ra, "_read_stored_session", side_effect=AssertionError("duplicate header read")):
            index = ra.build_archive_index(self.archive, header_cache=headers)
        self.assertIn(("wxid_sample_chat", "2030-01-02"), index.by_key)

    def test_multiple_renames_reuse_headers_from_the_existing_archive_scan(self):
        current = self.root / "current"
        for suffix in ("A", "B"):
            wxid = f"wxid_sample_{suffix}"
            self.old(directory=f"私聊_Former{suffix}", filename=f"Former{suffix}_20300102.json", wxid=wxid)
            data = payload(wxid=wxid)
            data["session"]["displayName"] = f"Current{suffix}"
            save(current / f"私聊_Current{suffix}_20300103" / f"Current{suffix}_20300103.json", data)
        for number in range(8):
            self.old(directory=f"私聊_Unrelated{number}", wxid=f"wxid_unrelated_{number}")
        with patch.object(ra, "_read_stored_session", wraps=ra._read_stored_session) as reader:
            result = reconcile_archive_session_names(self.archive, current,
                       find_guard_failures=archive_exports._find_guard_failures,
                       files_equal=archive_exports._files_equal)
        self.assertEqual(len(result.merges), 2)
        # The second merge may reopen the first merge's newly published file.
        self.assertLessEqual(reader.call_count, 1)

    def test_rename_scan_cache_preserves_unknown_directory_members(self):
        from wechat_diary_core.session_rename_alarm import _archive_session_directories
        self.old()
        broken = self.archive / "Unknown" / "bad_20300102.json"
        broken.parent.mkdir()
        broken.write_bytes(b"not json")
        headers = {}
        _archive_session_directories(self.archive, header_cache=headers)
        self.assertIsNone(headers["files"][broken.relative_to(self.archive).as_posix()]["session"])
        with patch.object(ra, "_read_stored_session", side_effect=AssertionError("unchanged file reopened")):
            index = ra.build_archive_index(self.archive, header_cache=headers)
        self.assertIn("Unknown", index.unknown)

    def test_rename_scan_does_not_cache_a_session_changed_during_its_read(self):
        from wechat_diary_core.session_rename_alarm import _archive_session_directories
        old = self.old()
        real_read = Path.read_text
        def read_then_replace(path, *args, **kwargs):
            text = real_read(path, *args, **kwargs)
            if path == old:
                replacement = save(old.with_suffix(".replacement"), payload(wxid="wxid_replaced_sample"))
                replacement.replace(old)
            return text
        headers = {}
        with patch.object(Path, "read_text", new=read_then_replace):
            _archive_session_directories(self.archive, header_cache=headers)
        self.assertNotIn(old.relative_to(self.archive).as_posix(), headers["files"])
        index = ra.build_archive_index(self.archive, header_cache=headers)
        self.assertEqual(index.members[old.parent.name], {"wxid_replaced_sample"})

    def test_cached_headers_detect_atomic_replacement_even_when_size_and_mtime_match(self):
        import os
        from wechat_diary_core.backends.weflow_api.backend import _stored_session_directory_collisions
        old = self.old()
        self.source()
        headers = {}
        _stored_session_directory_collisions(self.archive, header_cache=headers)
        info = old.stat()
        replaced = old.with_suffix(".replacement")
        replaced.write_bytes(old.read_bytes().replace(b"wxid_sample_chat", b"wxid_sample_peer"))
        os.utime(replaced, ns=(info.st_atime_ns, info.st_mtime_ns))
        os.replace(replaced, old)
        self.assertEqual((old.stat().st_size, old.stat().st_mtime_ns), (info.st_size, info.st_mtime_ns))
        plan = self.plan(header_cache=headers)
        self.assertEqual(plan.summary["resolutionBlocked"], 1)
        self.assertIn("wxid_sample_peer", plan.index.directories)

    def test_new_archive_member_is_not_hidden_by_a_header_cache(self):
        from wechat_diary_core.backends.weflow_api.backend import _stored_session_directory_collisions
        self.old()
        headers = {}
        _stored_session_directory_collisions(self.archive, header_cache=headers)
        self.old(filename="Other_20300101.json", wxid="wxid_other_sample")
        self.source()
        self.assertEqual(self.plan(header_cache=headers).summary["resolutionBlocked"], 1)
        with self.assertRaises(ValueError):
            ra.build_archive_index(self.root / "different", header_cache=headers)

    def test_runner_passes_headers_from_prepare_to_rotation(self):
        from datetime import date
        from unittest.mock import Mock
        from scripts.run_daily_export import run_daily_export
        from test_run_daily_export_script import _write_config, _quiet_deps
        from wechat_diary_core.config import load_config
        cfg = load_config(_write_config(self.root, target_users=""))
        deps = _quiet_deps(self.root)
        headers = {"root": str(self.archive), "files": {}}
        deps.backend.prepare_action = lambda: setattr(deps.backend, "archive_headers", headers)
        rotation = Mock(return_value=SimpleNamespace(target=None))
        deps.rotate_export_workspace = rotation
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            run_daily_export(cfg, deps=deps, day=date(2030, 1, 2))
        self.assertIs(rotation.call_args.kwargs["archive_headers"], headers)
        from scripts.run_daily_export import _open_session_rename_report
        self.assertIs(rotation.call_args.kwargs['report_opener'], _open_session_rename_report)


if __name__ == "__main__":
    unittest.main()
