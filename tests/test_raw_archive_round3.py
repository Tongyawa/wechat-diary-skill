from raw_isolation_support import isolated_file
"""Third-round regressions and a filesystem oracle independent of planner flags."""
from collections import Counter
from contextlib import redirect_stdout
import errno
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_raw_archive import payload, save
from wechat_diary_core import raw_archive as ra
from wechat_diary_core.backends.weflow_api.backend import _stored_session_directory_collisions
from wechat_diary_core.session_rename_alarm import reconcile_archive_session_names
from wechat_diary_core.workspace import merge_raw_exports_into_archive, rotate_export_workspace
from scripts import archive_exports


class RoundThreeArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "archived/raw"
        self.archive.mkdir(parents=True)
        self.live = self.root / "live"
        self.live.mkdir()

    def chat(self, root, directory, filename, *, ids=(1, 2), wxid="wxid_round3_sample"):
        return save(root / directory / filename, payload(ids=ids, wxid=wxid))

    def rotate(self, root=None):
        with redirect_stdout(io.StringIO()):
            return merge_raw_exports_into_archive(root or self.live, self.archive)

    def test_f1_renamed_subset_cannot_replace_resolved_canonical_file(self):
        old = self.chat(self.archive, "A", "A_20300101.json", ids=(1, 2, 3, 4, 5))
        before = old.read_bytes()
        incoming = self.chat(self.live, "B_20300101", "B_20300101.json")
        expected = incoming.read_bytes()
        self.rotate()
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual((self.archive / "B/B_20300101.json").read_bytes(), expected)
        self.assertFalse(incoming.exists())

    def test_f1_every_changed_old_chat_has_independent_coverage_or_prior_backup(self):
        layouts = [
            [("A", "A", (1, 2, 3, 4, 5))],
            [("A", "Former", (1, 2, 3))],
            [("A", "A", (1, 2, 3)), ("A", "Former", (1, 2, 3))],
            [("A", "A", (1, 2, 3)), ("A", "Former", (1, 2, 3, 4, 5))],
            [("A", "A", (1, 2, 3)), ("B", "B", (1, 2, 3, 4, 5))],
            [("A", "A", (1, 1, 2)), ("A", "Former", (1, 2, 3)), ("B", "Former", (1, 2, 3, 4, 5))],
        ]
        inputs = [("A", "A"), ("B", "B"), ("Fresh", "Fresh"), ("A", "Former")]
        identities = [(1, 2), (1, 2, 3), (1, 2, 3, 4, 5, 6), (1, 1, 2, 3, 4, 5, 6)]
        # The product includes the exact r3 shape: old A has five identities,
        # renamed incoming B has only the first two, under daily policy.
        cases = list(itertools.product(("daily", "bulk"), range(len(layouts)), inputs, identities))
        violations = []
        observed_operations = 0
        for case_number, (policy, layout_number, (directory, basename), ids) in enumerate(cases):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive, live = root / "archived/raw", root / "live"
                layout = layouts[layout_number]
                for old_dir, old_name, old_ids in layout:
                    self.chat(archive, old_dir, old_name + "_20300101.json", ids=old_ids)
                # These files must not be silently omitted from the before/after oracle.
                self.chat(archive, "A", "Unrelated_20291231.json", ids=(8,))
                self.chat(archive, "Other", "Other_20300101.json", ids=(9,), wxid="wxid_other_sample")
                incoming = self.chat(live, directory + "_20300101", basename + "_20300101.json", ids=ids)
                incoming_bytes = incoming.read_bytes()
                incoming_doc = json.loads(incoming_bytes)
                # Fixture suffix is known; do not call the implementation's path resolver.
                legacy = archive / incoming.parent.name.removesuffix("_20300101") / incoming.name
                archive_dirs = {d for d, _, _ in layout}
                chosen = next(iter(archive_dirs)) if len(archive_dirs) == 1 else directory if directory in archive_dirs else None
                canonical = archive / chosen / (chosen + '_20300101.json') if chosen else None
                before = {p: p.read_bytes() for p in archive.glob("*/*.json")}

                def counter(doc):
                    return Counter((m["createTime"], str(m["localId"])) for m in doc["messages"])

                def verify_protection(path, published, *, removal=False):
                    old = json.loads(before[path])
                    new = json.loads(published)
                    self.assertEqual(new["session"]["wxid"], old["session"]["wxid"])
                    self.assertFalse(counter(old) - counter(new), "changed old messages are absent from published bytes")
                    self.assertEqual(published, incoming_bytes, "publication must preserve the actual incoming bytes")
                    if not removal:
                        return  # Proven same-session/day coverage permits replacement.
                    relative = path.relative_to(archive).as_posix()
                    found = False
                    for manifest_path in (archive.parent / "raw-folded").glob("*/manifest.json"):
                        manifest = json.loads(manifest_path.read_bytes())
                        for item in manifest["items"]:
                            if item["path"] == relative:
                                saved = manifest_path.parent / relative
                                self.assertEqual(saved.read_bytes(), before[path])
                                self.assertEqual(item["sha256"], hashlib.sha256(before[path]).hexdigest())
                                self.assertTrue(item["coverage"]["covered"])
                                found = True
                    self.assertTrue(found, "old bytes must be backed up and readable before replacement/removal")

                original_replace, original_unlink = os.replace, Path.unlink

                def observe_replace(source, target):
                    nonlocal observed_operations
                    target = Path(target)
                    if target in before:
                        observed_operations += 1
                        verify_protection(target, Path(source).read_bytes())
                    return original_replace(source, target)

                def observe_unlink(path, *args, **kwargs):
                    nonlocal observed_operations
                    if path in before:
                        observed_operations += 1
                        surviving = [p for p in archive.glob("*/*.json")
                                     if p != path and p.read_bytes() == incoming_bytes]
                        self.assertTrue(surviving, "retirement must have surviving published bytes")
                        verify_protection(path, surviving[0].read_bytes(), removal=True)
                    return original_unlink(path, *args, **kwargs)

                try:
                    plan = ra.plan_raw_ingest(archive, [live], policy=policy)
                    blocked = False
                    with patch.object(os, "replace", new=observe_replace), patch.object(Path, "unlink", new=observe_unlink):
                        try:
                            ra.apply_raw_ingest_plan(plan, move_source=True, force_overwrite=True)
                        except ValueError:
                            blocked = True
                    independently_resolvable = len(archive_dirs) == 1 or directory in archive_dirs
                    independently_covered = all(not (Counter(old_ids) - Counter(ids)) for _, _, old_ids in layout)
                    normal_daily = independently_resolvable and independently_covered
                    fallback_safe = legacy not in before or not (counter(json.loads(before[legacy])) - counter(incoming_doc))
                    must_publish = (normal_daily or fallback_safe) if policy == 'daily' else independently_resolvable and independently_covered
                    if must_publish:
                        self.assertFalse(blocked)
                        self.assertTrue(any(p.read_bytes() == incoming_bytes for p in archive.glob("*/*.json")))
                        self.assertFalse(incoming.exists(), 'safe incoming must actually be consumed, not merely match pre-existing bytes')
                    elif policy == 'daily':
                        self.assertFalse(incoming.exists())
                        self.assertEqual(isolated_file(archive, live, incoming).read_bytes(), incoming_bytes)
                        self.assertEqual({p: p.read_bytes() for p in before}, before)
                    for path, old_bytes in before.items():
                        if not path.exists() or path.read_bytes() != old_bytes:
                            surviving = [p for p in archive.glob("*/*.json") if p.read_bytes() == incoming_bytes]
                            self.assertTrue(surviving)
                            verify_protection(path, surviving[0].read_bytes(), removal=not path.exists())
                except AssertionError as exc:
                    violations.append(f"case {case_number}: {policy}/{layout_number}/{directory}/{basename}/{ids}: {exc}")
        self.assertGreater(observed_operations, 0, "oracle must exercise real file replacements/removals")
        self.assertFalse(violations, f"{len(violations)} / {len(cases)} unsafe scenarios:\n" + "\n".join(violations[:8]))

    def make_fallback(self, wxid="wxid_round3_sample", prefix="First"):
        self.chat(self.archive, prefix + "A", prefix + "A_20300101.json", wxid=wxid)
        self.chat(self.archive, prefix + "B", prefix + "B_20300103.json", wxid=wxid)
        return self.chat(self.live, prefix + "C_20300102", prefix + "C_20300102.json", wxid=wxid)

    def test_f2_unrelated_clean_daily_and_bulk_do_not_erase_pending_report(self):
        self.make_fallback()
        self.rotate()
        report = self.archive.parent / "raw-fold-report.json"
        before = report.read_bytes()
        self.chat(self.live, "Other_20300104", "Other_20300104.json", wxid="wxid_other_sample")
        self.rotate()
        self.assertEqual(report.read_bytes(), before)
        self.chat(self.live, "Fresh_20300105", "Fresh_20300105.json", wxid="wxid_fresh_sample")
        plan = ra.plan_raw_ingest(self.archive, [self.live])
        result = ra.apply_raw_ingest_plan(plan)
        with redirect_stdout(io.StringIO()):
            ra.report_daily_result(plan, result)
        self.assertEqual(report.read_bytes(), before)

    def test_f2_only_an_observed_resolved_key_is_removed_and_bulk_is_isolated(self):
        self.make_fallback(prefix="First")
        self.make_fallback(wxid="wxid_other_sample", prefix="Other")
        self.rotate()
        report = self.archive.parent / "raw-fold-report.json"
        initial = report.read_bytes()
        incoming = self.chat(self.live, "FirstC_20300102", "FirstC_20300102.json")
        plan = ra.plan_raw_ingest(self.archive, [self.live])
        result = ra.apply_raw_ingest_plan(plan)
        with redirect_stdout(io.StringIO()):
            ra.report_daily_result(plan, result)
        self.assertEqual(report.read_bytes(), initial, "bulk must not rewrite the daily ledger")
        self.rotate()
        items = json.loads(report.read_bytes())["items"]
        identities = [i.get("identity") for i in items]
        self.assertEqual(identities, [{"wxid": "wxid_other_sample", "day": "2030-01-02"}])
        self.assertFalse(incoming.exists())

    def test_f2_report_membership_work_is_linear_for_large_successful_batches(self):
        class CountedPath(type(Path())):
            comparisons = 0
            def __eq__(self, other):
                type(self).comparisons += 1
                return super().__eq__(other)
            __hash__ = type(Path()).__hash__

        count = 256
        for kind in ("file", "chat"):
            with self.subTest(kind=kind):
                plan = ra.RawIngestPlan(self.archive, [self.live], "daily", ra.ArchiveIndex(self.archive))
                previous = {"items": []}
                for number in range(count):
                    source = CountedPath(self.live / f"Source{number}_20300101.json")
                    target = CountedPath(self.archive / f"Target{number}_20300101.json")
                    action = ra.RawAction(source, 0, source.name, target, kind, "synthetic", None,
                        wxid=f"wxid_report_sample_{number}", day="2030-01-01")
                    plan.actions.append(action)
                    item = {"entrypoint": "daily", "reasons": ["previous_issue"]}
                    if kind == "chat":
                        item["identity"] = {"wxid": action.wxid, "day": action.day}
                    else:
                        item["source"] = str(source)
                    previous["items"].append(item)
                result = ra.ApplyResult()
                result.published = [a.target for a in plan.actions]
                result.consumed = [a.source for a in plan.actions]
                # Isolate membership cost from the separately tested filesystem
                # observation. All old issues must still actually be consumed.
                with patch.object(ra, "_observed_day_files", return_value=(["A/A_20300101.json"], False)) as observe:
                    CountedPath.comparisons = 0
                    self.assertEqual(ra._updated_report_items(plan, result, previous), [])
                    work = CountedPath.comparisons
                    self.assertEqual(observe.call_count, count if kind == "chat" else 0)
                self.assertLess(work, count * 8, "report membership regressed to quadratic work")
        self.chat(self.live, "A_20300101", "A_20300101.json")
        output = io.StringIO()
        with patch.object(ra, "_updated_report_items", side_effect=AssertionError("readonly plan called report")):
            with redirect_stdout(output):
                code = archive_exports.main(["--print-plan", "--archive-raw-root", str(self.archive),
                                             "--raw-root", str(self.live)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["summary"]["chatFiles"], 1)

    def test_f3_bad_archive_body_falls_back_daily_but_is_hard_for_bulk(self):
        for malformed in ("truncated", "schema"):
            for renamed in (False, True):
                with self.subTest(malformed=malformed, renamed=renamed), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    archive, live = root / "archived/raw", root / "live"
                    old = self.chat(archive, "A", "A_20300101.json")
                    if malformed == "truncated":
                        raw = old.read_bytes()
                        old.write_bytes(raw[:len(raw) // 2])
                    else:
                        data = json.loads(old.read_bytes());data["messages"][0].pop("source")
                        save(old, data)
                    before = old.read_bytes()
                    name = "B" if renamed else "A"
                    incoming = self.chat(live, name + "_20300101", name + "_20300101.json", ids=(1, 2, 3))
                    expected = incoming.read_bytes()
                    self.assertEqual(ra.build_archive_index(archive).members["A"], {"wxid_round3_sample"})
                    bulk = ra.plan_raw_ingest(archive, [live])
                    with self.assertRaises(ValueError):
                        ra.apply_raw_ingest_plan(bulk, force_overwrite=True)
                    self.assertEqual(old.read_bytes(), before)
                    daily = ra.plan_raw_ingest(archive, [live], policy="daily")
                    self.assertEqual(daily.actions[0].resolution, "legacy_fallback")
                    ra.apply_raw_ingest_plan(daily, move_source=True)
                    self.assertEqual((archive / name / incoming.name).read_bytes(), expected)
                    self.assertFalse(incoming.exists())
                    if renamed:
                        self.assertEqual(old.read_bytes(), before)

    def test_f2_unreadable_previous_fallback_path_is_not_forgotten(self):
        incoming = self.chat(self.live, "", "Root_20300101.json")
        self.rotate()
        old = self.archive / incoming.name
        old.write_bytes(b"broken archived header")
        report = self.archive.parent / "raw-fold-report.json"
        for _ in range(2):
            self.chat(self.live, "A_20300101", "A_20300101.json")
            self.rotate()
            items = json.loads(report.read_bytes())["items"]
            self.assertEqual(len(items), 1)
            self.assertIn("archive_identity_unresolved", items[0]["reasons"])
        old.unlink()
        self.chat(self.live, "A_20300101", "A_20300101.json")
        self.rotate()
        self.assertEqual(json.loads(report.read_bytes())["items"], [])

    def test_f3_metadata_and_transcription_are_not_cas_dependencies(self):
        blocked = self.chat(self.live, "Blocked_20300101", "Blocked_20300101.json")
        peer = self.root / "peer"
        self.chat(peer, "Peer_20300101", "Peer_20300101.json")
        metadata_media = self.live / "_media/images/ab" / ("ab" * 16 + ".jpg")
        transcript_media = self.live / "_media/voices/cd" / ("cd" * 16 + ".mp3")
        content_media = self.live / "_media/images/ef" / ("ef" * 16 + ".png")
        media = (metadata_media, transcript_media, content_media)
        for path in media:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic media")
        references = ["../" + p.relative_to(self.live).as_posix() for p in media]
        data = json.loads(blocked.read_bytes())
        data["session"]["displayName"] = references[0]
        data["messages"][0].update(senderDisplayName=references[0], content=references[1],
            replyContext={"senderDisplayName": references[0], "content": references[0]})
        # The image preprocessor reads content image references regardless of type.
        data["messages"][1]["content"] = references[2]
        save(blocked, data)
        for _ in range(2):
            plan = ra.plan_raw_ingest(self.archive, [self.live, peer], policy="daily")
            ra.apply_raw_ingest_plan(plan, move_source=True)
            self.assertEqual({p for p in self.live.rglob("*") if p.is_file()}, set())
            self.assertTrue(isolated_file(self.archive, self.live, blocked).is_file())
            self.assertTrue(isolated_file(self.archive, self.live, content_media).is_file())
        for path in media:
            self.assertEqual((self.archive / path.relative_to(self.live)).read_bytes(), b"synthetic media")

    def test_f3_partial_incoming_uses_daily_fallback_when_target_is_absent(self):
        for messages in (None, 7, "not a message list", {"unexpected": "mapping"}):
            with self.subTest(messages=messages):
                incoming = self.chat(self.live, "A_20300101", "A_20300101.json")
                data = json.loads(incoming.read_bytes())
                data["messages"] = messages
                save(incoming, data)
                expected = incoming.read_bytes()
                self.rotate()
                self.assertFalse(incoming.exists())
                self.assertEqual((self.archive / "A/A_20300101.json").read_bytes(), expected)
                # Each shape exercises the expressly preserved empty-target case.
                (self.archive / 'A/A_20300101.json').unlink()

    def test_f3_repeated_blocked_daily_keeps_only_its_referenced_cas(self):
        blocked = self.chat(self.live, "Blocked_20300101", "Blocked_20300101.json")
        good = self.chat(self.live, "Good_20300102", "Good_20300102.json", wxid="wxid_good_sample")
        peer = self.root / "peer"
        self.chat(peer, "Peer_20300101", "Peer_20300101.json")
        media = []
        for content in (b"needed", b"shared", b"successful only", b"unreferenced"):
            digest = hashlib.md5(content).hexdigest()
            path = self.live / "_media/images" / digest[:2] / (digest + ".jpg")
            path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(content);media.append(path)
        for path, references in ((blocked, media[:2]), (good, media[1:3])):
            data = json.loads(path.read_bytes())
            for message, obj in zip(data["messages"], references):
                reference = "../" + obj.relative_to(self.live).as_posix()
                message.update(type="图片消息", source=reference, content=reference)
            save(path, data)
        for _ in range(2):
            plan = ra.plan_raw_ingest(self.archive, [self.live, peer], policy="daily")
            ra.apply_raw_ingest_plan(plan, move_source=True)
            self.assertEqual({p for p in self.live.rglob("*") if p.is_file()}, set())
            for p in (blocked, *media[:2]):
                self.assertTrue(isolated_file(self.archive, self.live, p).is_file())
        self.assertFalse(good.exists())
        for path in media:
            self.assertTrue((self.archive / path.relative_to(self.live)).is_file())

    def test_f4_bom_publication_can_be_indexed_and_does_not_hide_a_collision(self):
        incoming = self.chat(self.live, "A_20300101", "A_20300101.json")
        incoming.write_bytes(b"\xef\xbb\xbf" + incoming.read_bytes())
        expected = incoming.read_bytes()
        ra.apply_raw_ingest_plan(ra.plan_raw_ingest(self.archive, [self.live]))
        index = ra.build_archive_index(self.archive)
        self.assertFalse(index.unknown)
        self.assertEqual((self.archive / "A/A_20300101.json").read_bytes(), expected)
        self.assertEqual(_stored_session_directory_collisions(self.archive), {})
        other = self.chat(self.archive, "A", "Other_20300102.json", wxid="wxid_other_sample")
        other.write_bytes(b"\xef\xbb\xbf" + other.read_bytes())
        self.assertIn("A", _stored_session_directory_collisions(self.archive))

    def test_f4_rename_scan_also_accepts_bom_without_reencoding(self):
        old = self.chat(self.archive, "私聊_Former", "Former_20300101.json")
        old.write_bytes(b"\xef\xbb\xbf" + old.read_bytes());expected = old.read_bytes()
        self.chat(self.live, "私聊_Current_20300102", "Current_20300102.json")
        result = reconcile_archive_session_names(self.archive, self.live,
                    find_guard_failures=archive_exports._find_guard_failures, files_equal=archive_exports._files_equal)
        self.assertEqual(len(result.merges), 1)
        self.assertEqual((self.archive / "私聊_Current" / old.name).read_bytes(), expected)

    def test_f6_missing_daily_attachments_use_real_source_rename_without_full_payload_load(self):
        incoming = self.chat(self.live, "A_20300101", "A_20300101.json")
        cas = self.live / "_media/videos/ab" / ("ab" * 16 + ".mp4")
        legacy = incoming.parent / "media/image.jpg"
        for path in (cas, legacy):
            path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(b"synthetic attachment")
        media = {cas, legacy}
        original_read, original_replace = Path.read_bytes, os.replace
        moves = []
        def no_payload_load(path):
            if path in media:
                raise AssertionError("daily move loaded the entire attachment into memory")
            return original_read(path)
        def observe_replace(source, target):
            if Path(source) in media:
                moves.append((Path(source), Path(target)))
            return original_replace(source, target)
        cfg = SimpleNamespace(paths=SimpleNamespace(raw=self.live, processed=self.root / "processed", archived=self.archive.parent))
        with patch.object(Path, "read_bytes", new=no_payload_load), patch.object(os, "replace", new=observe_replace):
            with redirect_stdout(io.StringIO()):
                rotate_export_workspace(cfg)
        self.assertEqual({source for source, _ in moves}, media)
        for source, target in moves:
            self.assertFalse(source.exists())
            self.assertEqual(target.read_bytes(), b"synthetic attachment")

    def test_f6_cross_device_failure_falls_back_to_verified_copy(self):
        self.chat(self.live, "A_20300101", "A_20300101.json")
        media = self.live / "_media/images/ab" / ("ab" * 16 + ".jpg")
        media.parent.mkdir(parents=True);media.write_bytes(b"cross device sample")
        original = os.replace
        attempted = []
        def cross_device(source, target):
            if Path(source) == media:
                attempted.append(True)
                raise OSError(errno.EXDEV, "synthetic cross-device boundary")
            return original(source, target)
        with patch.object(os, "replace", new=cross_device):
            self.rotate()
        self.assertTrue(attempted)
        self.assertFalse(media.exists())
        self.assertEqual((self.archive / media.relative_to(self.live)).read_bytes(), b"cross device sample")


if __name__ == "__main__":
    unittest.main()
