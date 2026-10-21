"""Final-destination invariants: generated layouts and gate mutation checks."""
from collections import Counter
from contextlib import redirect_stdout, nullcontext
from datetime import date
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_raw_archive import payload, save, tree
from raw_isolation_support import isolated_file, assert_self_contained
import test_moments_ranges as range_fixtures
from wechat_diary_core import raw_archive as ra
from wechat_diary_core.workspace import rotate_export_workspace


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def document(raw):
    try:
        value = json.loads(raw.decode('utf-8-sig'))
        if (not isinstance(value, dict) or not isinstance(value.get('session'), dict)
                or not isinstance(value.get('messages'), list)):
            return None
        if not all(k in value['session'] for k in ('wxid', 'nickname', 'remark', 'displayName', 'type')):
            return None
        if not all(isinstance(m, dict) and all(k in m for k in ('localId', 'createTime', 'source', 'type'))
                   for m in value['messages']):
            return None
        return value
    except (ValueError, UnicodeError):
        return None


def independent_coverage(old_path, old_bytes, new_path, new_bytes):
    """Contract oracle: no imports of production comparison or path resolution."""
    old, new = document(old_bytes), document(new_bytes)
    day = lambda p: re.search(r'_(\d{8})\.json$', p.name, re.I)
    a, b = day(old_path), day(new_path)
    if not old or not new or not a or not b or a[1] != b[1]:
        return False
    if old['session']['wxid'] != new['session']['wxid']:
        return False
    if old_bytes == new_bytes:
        return True
    def lineage(doc):
        meta = doc.get('weflow', {})
        if str(meta.get('source', '')).lower() == 'http_api':
            return 'api'
        if str(meta.get('generator', '')).lower() == 'weflow':
            return 'gui'
        return None
    if lineage(old) and lineage(old) == lineage(new):
        counter = lambda d: Counter((int(m['createTime']), str(m['localId'])) for m in d['messages'])
    else:
        if any(not m.get('platformMessageId') for d in (old, new) for m in d['messages']):
            return False
        counter = lambda d: Counter(str(m['platformMessageId']) for m in d['messages'])
        if not counter(old) & counter(new):
            return False
    return not (counter(old) - counter(new))


def observe_execution(archive, live, policy, *, disable_gate=False, forced_target=None):
    before = {p: p.read_bytes() for p in archive.rglob('*') if p.is_file()}
    inputs = {p: p.read_bytes() for p in live.rglob('*') if p.is_file()}
    violations, operations = [], []
    real_replace, real_unlink = os.replace, Path.unlink
    gate = getattr(ra, '_final_target_check', None)

    def inspect(plan, action, **kwargs):
        if forced_target is not None and action.source in inputs and action.source.suffix == '.json':
            action.target = forced_target
            action.target_before = digest(before[forced_target])
            action.existing.clear()
            action.retire.clear()
        if not disable_gate:
            gate(plan, action, **kwargs)

    def backed(path):
        for manifest_path in (archive.parent / 'raw-folded').glob('*/manifest.json'):
            data = json.loads(manifest_path.read_bytes())
            for item in data['items']:
                if archive / item['path'] == path:
                    saved = manifest_path.parent / item['path']
                    if saved.is_file() and saved.read_bytes() == before[path] and item['sha256'] == digest(before[path]):
                        return True
        return False

    def replace(source, target):
        target = Path(target)
        if target in before:
            incoming = Path(source).read_bytes()
            operations.append(('replace', target))
            if not (incoming == before[target] or independent_coverage(target, before[target], target, incoming) or backed(target)):
                violations.append(('unprotected_replace', target.name))
        return real_replace(source, target)

    def unlink(path, *args, **kwargs):
        if path in before:
            operations.append(('unlink', path))
            if not backed(path):
                violations.append(('unbacked_removal', path.name))
        return real_unlink(path, *args, **kwargs)

    with patch.object(ra, '_final_target_check', side_effect=inspect) if gate else nullcontext():
        plan = ra.plan_raw_ingest(archive, [live], policy=policy)
    with patch.object(os, 'replace', new=replace), patch.object(Path, 'unlink', new=unlink):
        try:
            result = ra.apply_raw_ingest_plan(plan, move_source=True, force_overwrite=True)
        except ValueError as exc:
            if policy != 'bulk' or 'raw plan has hard blockers' not in str(exc):
                raise
            result = None
    for path, value in before.items():
        if not path.exists() and not backed(path):
            violations.append(('missing_old_bytes', path.name))
        elif path.exists() and path.read_bytes() != value and not (
                independent_coverage(path, value, path, path.read_bytes()) or backed(path)):
            violations.append(('changed_old_bytes', path.name))
    for action in plan.actions:
        if action.status != 'ready':
            if policy == 'daily' and result is not None:
                if action.source.exists():
                    violations.append(('blocked_source_still_live', action.relative))
                try:
                    saved = isolated_file(archive, live, action.source)
                    if saved.read_bytes() != inputs[action.source]:
                        violations.append(('changed_isolated_bytes', action.relative))
                except AssertionError:
                    violations.append(('missing_isolated_source', action.relative))
            elif not action.source.is_file() or action.source.read_bytes() != inputs[action.source]:
                violations.append(('lost_blocked_source', action.relative))
        elif result is not None and action.target.read_bytes() != inputs[action.source]:
            violations.append(('wrong_published_bytes', action.relative))
    if result is not None and policy == 'daily' and result.isolation_dir:
        for action in plan.actions:
            if action.source not in result.retained:
                continue
            for reference in action.cas_references:
                target = result.isolation_dir / reference.relative_to(live)
                if not target.is_file():
                    violations.append(('missing_isolated_cas', action.relative))
    return plan, result, violations, len(operations)


class FinalTargetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.archive, self.live = self.root / 'archived/raw', self.root / 'live'
        self.archive.mkdir(parents=True)
        self.live.mkdir()

    def chat(self, root, relative, ids=(1, 2), **kwargs):
        return save(root / relative, payload(ids, **kwargs))

    def test_identical_unknown_lineage_is_noop_or_backed_retirement(self):
        for old_name in ('A_20300101.json', 'Former_20300101.json'):
            with self.subTest(old_name=old_name), tempfile.TemporaryDirectory() as folder:
                archive, live = Path(folder) / 'archived/raw', Path(folder) / 'live'
                doc = payload((1, 2))
                del doc['weflow']
                for message in doc['messages']:
                    del message['platformMessageId']
                old = save(archive / 'A' / old_name, doc)
                incoming = save(live / 'A_20300101/A_20300101.json', doc)
                original = old.read_bytes()
                plan = ra.plan_raw_ingest(archive, [live])
                action, = plan.actions
                self.assertEqual(action.status, 'ready', action.reasons)
                self.assertEqual(action.existing[0]['coverage']['basis'], 'identical_sha256')
                self.assertEqual(action.noop, old_name.startswith('A_'))
                result = ra.apply_raw_ingest_plan(plan, move_source=True)
                self.assertFalse(result.retained)
                self.assertFalse(incoming.exists())
                self.assertEqual(action.target.read_bytes(), original)
                manifests = list((archive.parent / 'raw-folded').glob('*/manifest.json'))
                if old == action.target:
                    self.assertFalse(manifests)
                else:
                    self.assertFalse(old.exists())
                    self.assertEqual(len(manifests), 1)
                    self.assertEqual((manifests[0].parent / 'A' / old_name).read_bytes(), original)

    def test_different_bytes_unknown_lineage_still_requires_identity_proof(self):
        doc = payload((1, 2))
        del doc['weflow']
        for message in doc['messages']:
            del message['platformMessageId']
        old = save(self.archive / 'A/A_20300101.json', doc)
        incoming = save(self.live / 'A_20300101/A_20300101.json', doc)
        incoming.write_bytes(incoming.read_bytes() + b'\n')
        plan = ra.plan_raw_ingest(self.archive, [self.live])
        action, = plan.actions
        self.assertEqual(action.status, 'hardBlocked')
        self.assertIn('coverage_failed', action.reasons)
        self.assertFalse(action.noop)
        before = old.read_bytes()
        with self.assertRaisesRegex(ValueError, 'hard blockers'):
            ra.apply_raw_ingest_plan(plan, force_overwrite=True)
        self.assertEqual(old.read_bytes(), before)
        self.assertTrue(incoming.exists())

    def fixed(self, shape):
        if shape == 'q1':
            self.chat(self.archive, 'X/X_20300101.json', (1, 2, 3, 4, 5))
            self.chat(self.live, 'A_20300101/A_20300102.json', (1, 2))
            bad = self.live / 'A_20300101/X_20300101.json'
            bad.write_bytes(b'{"session":')
        elif shape == 'p1':
            self.chat(self.archive, 'A/A_20300101.json', (5, 6), wxid='wxid_victim')
            self.chat(self.live, 'A_20300101/A_20300101.json', (1,))
        elif shape == 'p2':
            self.chat(self.archive, 'A/Former_20300101.json', (1, 2, 3))
            self.chat(self.live, 'A_20300101/Former_20300101.json', (1, 2))
        else:
            old = self.chat(self.archive, 'A/A_20300101.json', (1, 2, 3, 4, 5))
            self.chat(self.live, 'B_20300101/B_20300101.json', (1, 2))
            return old
        return None

    def test_gate_mutation_is_seen_in_q1_p1_p2_and_r3_route_regression(self):
        for shape in ('q1', 'p1', 'p2', 'r3'):
            for disabled in (False, True):
                with self.subTest(shape=shape, disabled=disabled), tempfile.TemporaryDirectory() as folder:
                    original_archive, original_live = self.archive, self.live
                    self.archive, self.live = Path(folder) / 'archived/raw', Path(folder) / 'live'
                    self.archive.mkdir(parents=True)
                    self.live.mkdir()
                    forced = self.fixed(shape)
                    plan, result, violations, operations = observe_execution(
                        self.archive, self.live, 'daily', disable_gate=disabled, forced_target=forced)
                    self.assertEqual(bool(violations), disabled, (shape, violations))
                    if disabled:
                        self.assertGreater(operations, 0)
                    self.archive, self.live = original_archive, original_live

    def test_r3_original_route_still_safely_publishes_by_name(self):
        old = self.fixed('r3')
        before = old.read_bytes()
        plan, result, errors, _ = observe_execution(self.archive, self.live, 'daily')
        self.assertFalse(errors)
        self.assertEqual(old.read_bytes(), before)
        self.assertTrue((self.archive / 'B/B_20300101.json').exists())

    def test_q1_q1b_final_target_is_preserved_and_reported_without_backup(self):
        for bad in (b'{"session":', b'{}', b'[]'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as folder:
                archive, live = Path(folder) / 'archived/raw', Path(folder) / 'live'
                old = self.chat(archive, 'X/X_20300101.json', (1, 2, 3))
                before = old.read_bytes()
                self.chat(live, 'A_20300101/A_20300102.json')
                source = live / 'A_20300101/X_20300101.json'
                source.write_bytes(bad)
                plan, result, violations, _ = observe_execution(archive, live, 'daily')
                self.assertFalse(violations)
                self.assertEqual(old.read_bytes(), before)
                self.assertEqual(isolated_file(archive, live, source).read_bytes(), bad)
                self.assertFalse((archive.parent / 'raw-folded').exists())
                with redirect_stdout(io.StringIO()):
                    ra.report_daily_result(plan, result)
                report = json.loads((archive.parent / 'raw-fold-report.json').read_bytes())
                self.assertIn(str(old), json.dumps(report).replace('\\\\', '\\'))
                self.assertIn('invalid_incoming_chat', json.dumps(report))
                # A different date in the same incoming directory still publishes.
                self.assertTrue((archive / 'X/X_20300102.json').exists())

    def test_changed_plan_cannot_write_an_uninspected_target(self):
        self.chat(self.live, 'A_20300101/A_20300101.json')
        old = self.chat(self.archive, 'Victim/Victim_20300101.json', (5,), wxid='wxid_other')
        before = tree(self.archive)
        plan = ra.plan_raw_ingest(self.archive, [self.live], policy='daily')
        plan.actions[0].target = old
        plan.actions[0].target_before = digest(old.read_bytes())
        with self.assertRaisesRegex(ValueError, 'final target inspection'):
            ra.apply_raw_ingest_plan(plan)
        self.assertEqual(tree(self.archive), before)

    def test_q7_corrupt_report_is_preserved_and_new_victim_is_recorded(self):
        self.fixed('p1')
        report = self.archive.parent / 'raw-fold-report.json'
        report.write_bytes(b'{broken original ledger')
        plan, result, violations, _ = observe_execution(self.archive, self.live, 'daily')
        self.assertFalse(violations)
        output = io.StringIO()
        with redirect_stdout(output):
            ra.report_daily_result(plan, result)
        corrupt, = report.parent.glob('raw-fold-report.corrupt-*.json')
        self.assertEqual(corrupt.read_bytes(), b'{broken original ledger')
        self.assertIn(str(corrupt), output.getvalue())
        self.assertIn('wxid_victim', report.read_text(encoding='utf-8'))
        self.assertTrue(report.with_suffix('.md').is_file())

    def test_retained_raw_skips_rendered_days_but_not_a_filename_only_day(self):
        self.fixed('p1')
        processed = self.root / 'processed'
        # The renderer emits Jan 2. A filename-only Jan 1 output is not touched
        # under the new shared-target contract; keep this precision counterexample.
        cfg = SimpleNamespace(paths=SimpleNamespace(raw=self.live, processed=processed,
            archived=self.archive.parent), daily_export=SimpleNamespace(
                target_usernames=['wxid_sample_chat'], target_processed_subroot='_targets'))
        from wechat_diary_core.archiving import archive
        from wechat_diary_core.preprocessing import ProcessedChatExport
        source, = self.live.rglob('*.json')
        export = ProcessedChatExport(source, source.parent.name, json.loads(source.read_text(encoding='utf-8-sig')))
        probe_root = self.root / 'render-probe'
        rendered = archive([export], config=cfg, output_root=probe_root)
        self.assertEqual([p.relative_to(probe_root).as_posix() for p in rendered], ['A/2030-01-02.md'])
        victims = ['A/2030-01-02.md', '_targets/chats/2030-01-02.md']
        untouched = ['A/2030-01-01.md', 'Other/2030-01-03.md']
        for relative in [*victims, *untouched]:
            path = processed / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'incoming render')
            old = self.archive.parent / 'processed' / relative
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_bytes(b'archived render')
        with redirect_stdout(io.StringIO()):
            rotate_export_workspace(cfg)
        for relative in victims:
            self.assertEqual((self.archive.parent / 'processed' / relative).read_bytes(), b'archived render')
            self.assertTrue((processed / relative).exists())
        for relative in untouched:
            self.assertEqual((self.archive.parent / 'processed' / relative).read_bytes(), b'incoming render')
            self.assertFalse((processed / relative).exists())

    def test_original_q1_noncanonical_victim_basename(self):
        self.chat(self.archive, 'X/X_20300101.json', (1,))
        victim = self.chat(self.archive, 'X/A_20300102.json', (1, 2, 3, 4, 5))
        before = victim.read_bytes()
        self.chat(self.live, 'A_20300101/A_20300101.json', (1, 2))
        bad = self.live / 'A_20300101/A_20300102.json'
        bad.write_bytes(b'{"session":')
        plan, result, errors, _ = observe_execution(self.archive, self.live, 'daily')
        self.assertFalse(errors)
        self.assertEqual(victim.read_bytes(), before)
        self.assertFalse(bad.exists())
        self.assertTrue(isolated_file(self.archive, self.live, bad).exists())
        self.assertFalse((self.archive.parent / 'raw-folded').exists())
        self.assertEqual(len(document((self.archive / 'X/X_20300101.json').read_bytes())['messages']), 2)
        with redirect_stdout(io.StringIO()):
            ra.report_daily_result(plan, result)
        report = json.loads((self.archive.parent / 'raw-fold-report.json').read_bytes())
        self.assertTrue(any(d['legacyTarget']['path'] == str(victim)
                            for item in report['items'] for d in item.get('details', [])))

    def test_q2_bulk_directory_collision_includes_legacy_media_and_writes_nothing(self):
        self.chat(self.live, 'D_20300101/D_20300101.json', wxid='wxid_first')
        self.chat(self.live, 'D_20300102/D_20300102.json', wxid='wxid_second')
        media = self.live / 'D_20300101/media/images/x.jpg'
        media.parent.mkdir(parents=True)
        media.write_bytes(b'legacy media')
        before = tree(self.archive), tree(self.live)
        plan = ra.plan_raw_ingest(self.archive, [self.live])
        self.assertTrue(all(a.status != 'ready' for a in plan.actions))
        self.assertTrue(all('planned_directory_multiple_wxids' in a.reasons
                            for a in plan.actions if a.kind == 'chat'))
        self.assertIn('session_directory_blocked', next(a for a in plan.actions if a.source == media).reasons)
        with self.assertRaises(ValueError):
            ra.apply_raw_ingest_plan(plan, force_overwrite=True)
        self.assertEqual((tree(self.archive), tree(self.live)), before)

    def test_q3_three_rotations_keep_only_affected_source_and_do_not_repeat_report(self):
        victim = self.chat(self.archive, 'A/A_20300101.json', (7, 8, 9), wxid='wxid_victim')
        before = victim.read_bytes()
        source = self.chat(self.live, 'A_20300101/A_20300101.json', (1,))
        data = json.loads(source.read_bytes())
        reference = '../_media/images/ab/' + 'ab' * 16 + '.jpg'
        data['messages'][0]['source'] = reference
        save(source, data)
        cas = (source.parent / reference).resolve()
        cas.parent.mkdir(parents=True)
        cas.write_bytes(b'needed by retained source')
        processed = self.root / 'processed'
        cfg = SimpleNamespace(paths=SimpleNamespace(raw=self.live, processed=processed, archived=self.archive.parent))
        opener = Mock()
        report_before = None
        for day in (3, 4, 5):
            fresh = self.chat(self.live, f'Fresh_2030010{day}/Fresh_2030010{day}.json', wxid='wxid_fresh')
            with redirect_stdout(io.StringIO()):
                rotate_export_workspace(cfg, report_opener=opener)
            self.assertFalse(fresh.exists())
            self.assertFalse(source.exists() or cas.exists())
            self.assertTrue(isolated_file(self.archive, self.live, source).exists())
            self.assertTrue(isolated_file(self.archive, self.live, cas).exists())
            self.assertEqual(victim.read_bytes(), before)
            report = tuple((self.archive.parent / name).read_bytes()
                           for name in ('raw-fold-report.json', 'raw-fold-report.md'))
            if report_before is not None:
                self.assertEqual(report, report_before)
            report_before = report
        self.assertEqual(opener.call_count, 1)

    @unittest.skipUnless(os.name == 'nt', 'case-insensitive filesystem')
    def test_q4_all_four_case_only_layouts_preserve_uncovered_old_bytes(self):
        shapes = [
            ('abc_20300101.json', 'wxid_victim', 'ABC', 'ABC', None),
            ('Abc_20300101.json', 'wxid_sample_chat', 'aBC', 'Former', ('Abc', 'Former_20300101.json', 'wxid_sample_chat')),
            ('Abc_20300101.json', 'wxid_sample_chat', 'ABC', 'abc', ('Zed', 'Zed_20300102.json', 'wxid_sample_chat')),
            ('Abc_20300101.json', 'wxid_sample_chat', 'ABC', 'abc', ('Abc', 'Other_20300102.json', 'wxid_other')),
        ]
        for filename, wxid, directory, basename, extra in shapes:
            with self.subTest(shape=filename + basename + wxid), tempfile.TemporaryDirectory() as folder:
                archive, live = Path(folder) / 'archived/raw', Path(folder) / 'live'
                self.chat(archive, 'Abc/' + filename, (1, 2, 3), wxid=wxid)
                if extra:
                    self.chat(archive, extra[0] + '/' + extra[1], (1, 2, 3, 4), wxid=extra[2])
                source = self.chat(live, f'{directory}_20300101/{basename}_20300101.json', (1,))
                before = tree(archive)
                plan, result, errors, _ = observe_execution(archive, live, 'daily')
                self.assertFalse(errors)
                self.assertEqual(tree(archive), before)
                self.assertFalse(source.exists())
                self.assertTrue(isolated_file(archive, live, source).exists())

    def test_q6_two_identities_same_proposed_target_are_checked_individually(self):
        for exists in (False, True):
            with self.subTest(exists=exists), tempfile.TemporaryDirectory() as folder:
                archive, live = Path(folder) / 'archived/raw', Path(folder) / 'live'
                self.chat(archive, 'A/A_20291231.json', (5,), wxid='wxid_second')
                target = archive / 'A/A_20300101.json'
                if exists:
                    self.chat(archive, 'A/A_20300101.json', (1, 2, 3), wxid='wxid_second')
                old_bytes = target.read_bytes() if exists else None
                first = self.chat(live, 'A_20300101/A_20300101.json', (1,), wxid='wxid_first')
                second = self.chat(live, 'B_20300101/B_20300101.json', (1, 2, 3, 4), wxid='wxid_second')
                plan, result, errors, _ = observe_execution(archive, live, 'daily')
                self.assertFalse(errors)
                actions = {a.source: a for a in plan.actions}
                self.assertEqual(actions[first].target, target)
                self.assertEqual(actions[second].target, archive / 'B/B_20300101.json')
                self.assertFalse(first.exists())
                if exists:
                    self.assertTrue(isolated_file(archive, live, first).exists())
                self.assertFalse(second.exists())
                self.assertEqual(actions[first].status == 'ready', not exists)
                self.assertEqual(actions[second].status, 'ready')
                self.assertEqual(document(target.read_bytes())['session']['wxid'],
                                 'wxid_second' if exists else 'wxid_first')
                if exists:
                    self.assertEqual(target.read_bytes(), old_bytes)

    def test_q7_markdown_failure_recovers_and_v1_evidence_survives(self):
        self.fixed('p1')
        opener = Mock()
        original = ra._atomic_bytes
        def fail_md(path, data):
            if path.suffix == '.md':
                raise OSError('synthetic markdown failure')
            return original(path, data)
        plan = ra.plan_raw_ingest(self.archive, [self.live], policy='daily')
        result = ra.apply_raw_ingest_plan(plan, move_source=True)
        with patch.object(ra, '_atomic_bytes', side_effect=fail_md), redirect_stdout(io.StringIO()):
            ra.report_daily_result(plan, result, report_opener=opener)
        report = self.archive.parent / 'raw-fold-report.json'
        self.assertTrue(report.exists())
        self.assertFalse(report.with_suffix('.md').exists())
        opener.assert_not_called()
        for _ in range(2):
            with redirect_stdout(io.StringIO()):
                ra.report_daily_result(plan, result, report_opener=opener)
        self.assertEqual(opener.call_count, 1)
        legacy = {'entrypoint': 'daily', 'items': [
            {'source': 'old-live/Z_20290101/Z_20290101.json', 'target': 'old-archive/Z/Z_20290101.json',
             'reasons': ['coverage_failed_keep_old:Z/Old.json']},
            {'directory': 'Old', 'reasons': ['legacy_media_retained'], 'remainingFiles': ['Old/media/a.jpg']}]}
        report.write_text(json.dumps(legacy), encoding='utf-8')
        self.chat(self.live, 'Fresh_20300102/Fresh_20300102.json', wxid='wxid_fresh')
        for _ in range(2):
            plan = ra.plan_raw_ingest(self.archive, [self.live], policy='daily')
            result = ra.apply_raw_ingest_plan(plan, move_source=True)
            with redirect_stdout(io.StringIO()):
                ra.report_daily_result(plan, result, report_opener=opener)
        saved = json.loads(report.read_bytes())['items']
        self.assertTrue(any(i.get('source') == legacy['items'][0]['source'] for i in saved))
        self.assertTrue(any(i.get('directory') == 'Old' for i in saved))
        self.assertEqual(opener.call_count, 2)

    def test_r4_conflicting_attachment_print_plan_is_read_only(self):
        from scripts import archive_exports
        self.chat(self.archive, 'A/B_20300101.json', (1,))
        self.chat(self.live, 'A_20300101/A_20300101.json', (1, 2), generation='pre')
        for root, directory, value in ((self.archive, 'A', b'old'), (self.live, 'A_20300101', b'new')):
            path = root / directory / 'media/x.jpg'
            path.parent.mkdir(parents=True)
            path.write_bytes(value)
        before = tree(self.archive), tree(self.live)
        output = io.StringIO()
        with redirect_stdout(output):
            code = archive_exports.main(['--print-plan', '--archive-raw-root', str(self.archive),
                                         '--raw-root', str(self.live)])
        self.assertEqual(code, 1)
        self.assertEqual((tree(self.archive), tree(self.live)), before)
        plan = json.loads(output.getvalue())
        self.assertTrue(all(a['status'] != 'ready' for a in plan['chatItems'] + plan['nonChatActions']))

    def generated(self, policy):
        rng = random.Random(2026101805)
        mutations = 0
        kinds = set()
        shapes = [(1,), (1, 2, 3), (1, 2, 3, 4, 5), (2, 4), (1, 1, 2, 3)]
        for number in range(500):
            with self.subTest(policy=policy, number=number), tempfile.TemporaryDirectory() as folder:
                archive, live = Path(folder) / 'archived/raw', Path(folder) / 'live'
                archive.mkdir(parents=True)
                live.mkdir()
                for old_number in range(rng.randrange(4)):
                    directory = rng.choice(('A', 'B'))
                    name = ('A', 'Former', 'Legacy')[old_number]
                    old = self.chat(archive, f'{directory}/{name}_20300101.json', rng.choice(shapes),
                                    generation=rng.choice(('cas', 'pre', 'legacy')))
                    if rng.randrange(4) == 0:
                        old.write_bytes(b'\xef\xbb\xbf' + old.read_bytes())
                if rng.randrange(3) == 0:
                    self.chat(archive, 'Fresh/Fresh_20300101.json', (7, 8), wxid='wxid_other')
                directories = rng.sample(['A', 'B', 'Fresh', 'a'], rng.randint(1, 3))
                for directory in directories:
                    for file_number in range(rng.randint(1, 3)):
                        name = (directory, 'Former', 'A')[file_number]
                        kind = rng.choice(('chat', 'bom', 'truncated', 'nonchat', 'media', 'cas'))
                        kinds.add(kind)
                        if kind in ('media', 'cas'):
                            relative = (f'{directory}_20300101/media/asset-{file_number}.bin' if kind == 'media'
                                        else '_media/images/ab/' + 'ab' * 16 + '.jpg')
                            path = live / relative
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(b'synthetic media')
                        else:
                            path = self.chat(live, f'{directory}_20300101/{name}_20300101.json', rng.choice(shapes),
                                              generation=rng.choice(('cas', 'pre', 'legacy')))
                            if kind == 'bom':
                                path.write_bytes(b'\xef\xbb\xbf' + path.read_bytes())
                            elif kind == 'truncated':
                                path.write_bytes(path.read_bytes()[:-23])
                            elif kind == 'nonchat':
                                path.write_bytes(rng.choice((b'{}', b'[]', b'{"posts": []}')))
                # Ensure the random suite also exercises successful mutations,
                # rather than passing solely because compound inputs block.
                if number % 10 == 0:
                    self.chat(archive, 'Safe/Safe_20300102.json', (1,))
                    self.chat(live, 'Safe_20300102/Safe_20300102.json', (1, 2, 3))
                plan, result, errors, count = observe_execution(archive, live, policy)
                self.assertFalse(errors, errors)
                mutations += count
        self.assertEqual(kinds, {'chat', 'bom', 'truncated', 'nonchat', 'media', 'cas'})
        self.assertGreater(mutations, 0)

    def test_generated_daily_500(self):
        self.generated('daily')

    def test_generated_bulk_500(self):
        self.generated('bulk')


class RangePlanBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = range_fixtures.MomentsRangeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def batch_comparison(self, *, covered=True, initial=False, initial_missing=False, shared_media=False, restore_first=False):
        config = range_fixtures._config(self.fixture.root)
        archive = range_fixtures.load_config(config).paths.archived / 'raw'
        archive.mkdir(parents=True, exist_ok=True)
        expected = self.fixture.root / 'legacy-oracle'
        expected.mkdir()
        first, first_doc = self.fixture.snapshot(self.fixture.root / 'first', ids=('first', 'second'), day=date(2010, 1, 1))
        second, second_doc = self.fixture.snapshot(self.fixture.root / 'second',
            ids=('first', 'second', 'third') if covered else ('other',))
        initial_bytes = None
        if initial:
            old, _ = self.fixture.snapshot(archive, ids=('first', 'missing') if initial_missing else ('first',), day=date(2010, 1, 1))
            initial_bytes = old.read_bytes()
            (expected / old.name).write_bytes(initial_bytes)
        if shared_media:
            ref = self.fixture.media(first.parent)
            for path, doc in ((first, first_doc), (second, second_doc)):
                doc['posts'][0]['media'][0]['localPath'] = ref
                self.fixture.save(path, doc)
            # The legacy entrypoint lands all attachments before any range.
            shutil.copytree(first.parent / '_media', expected / '_media')
        sources = [first, second]
        if restore_first:
            third = self.fixture.root / 'third' / first.name
            third.parent.mkdir()
            third.write_bytes(first.read_bytes())
            sources.append(third)
        review = False
        with redirect_stdout(io.StringIO()):
            for source in sources:
                review |= range_fixtures.ranges.publish_range(source, expected / source.name).needs_review
        roots = [p.parent for p in sources]
        before = [tree(p) for p in [archive, *roots]]
        plan = ra.plan_raw_ingest(archive, roots)
        self.assertEqual(before, [tree(p) for p in [archive, *roots]])
        self.assertTrue(all(a.status == 'ready' for a in plan.actions), plan.to_dict())
        with redirect_stdout(io.StringIO()):
            code = range_fixtures.archive_exports.main(['--config', str(config),
                *(part for path in roots for part in ('--raw-root', str(path)))])
        self.assertEqual(code, 3 if review else 0)
        snapshot = lambda root: {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        self.assertEqual(snapshot(archive), snapshot(expected))
        if covered:
            last = next(a for a in plan.actions if a.source == second)
            first_action = next(a for a in plan.actions if a.source == first)
            self.assertIn(first_action.target, last.retire)
            manifest_path, = (archive.parent / 'raw-folded').glob('*/manifest.json')
            entries = json.loads(manifest_path.read_bytes())['items']
            backed = {p['sha256']: (manifest_path.parent / p.get('backupPath', p['path'])).read_bytes() for p in entries}
            self.assertEqual(backed[digest(first.read_bytes())], first.read_bytes())
            if initial and not initial_missing:
                self.assertEqual(backed[digest(initial_bytes)], initial_bytes)
        return plan

    def test_batch_later_covering_range_matches_legacy_final_files(self):
        self.batch_comparison()

    def test_batch_disjoint_ranges_match_legacy_review_exit(self):
        self.batch_comparison(covered=False)

    def test_batch_replacement_then_retirement_keeps_both_byte_versions(self):
        self.batch_comparison(initial=True)

    def test_batch_range_media_can_come_from_another_input_root(self):
        self.batch_comparison(shared_media=True)

    def test_batch_retired_path_can_be_published_again(self):
        self.batch_comparison(restore_first=True)

    def test_batch_generated_retention_name_is_later_retired(self):
        self.batch_comparison(initial=True, initial_missing=True)

    def test_batch_same_basename_different_bytes_keeps_legacy_preflight_block(self):
        archive = self.fixture.root / 'archived/raw'
        archive.mkdir(parents=True)
        first, _ = self.fixture.snapshot(self.fixture.root / 'first')
        second, _ = self.fixture.snapshot(self.fixture.root / 'second', ids=('first', 'second'))
        self.assertEqual(first.name, second.name)
        plan = ra.plan_raw_ingest(archive, [first.parent, second.parent])
        self.assertTrue(all('multiple_incoming_for_target' in a.reasons for a in plan.actions))
        with self.assertRaisesRegex(ValueError, 'hard blockers'):
            ra.apply_raw_ingest_plan(plan)
        self.assertFalse(list(archive.iterdir()))

    def future_pair(self, policy='bulk'):
        archive = self.fixture.root / 'archived/raw'
        archive.mkdir(parents=True, exist_ok=True)
        first, _ = self.fixture.snapshot(self.fixture.root / 'first', day=date(2010, 1, 1))
        second, _ = self.fixture.snapshot(self.fixture.root / 'second', ids=('first', 'second'))
        return ra.plan_raw_ingest(archive, [first.parent, second.parent], policy=policy), first, second

    def test_future_retirement_backup_is_verified_before_unlink(self):
        plan, first, second = self.future_pair()
        future = plan.archive / first.name
        original = Path.unlink
        observed = []
        def unlink(path, *args, **kwargs):
            if path == future:
                manifest_path, = (plan.archive.parent / 'raw-folded').glob('*/manifest.json')
                entries = json.loads(manifest_path.read_bytes())['items']
                record, = [p for p in entries if p['path'] == first.name]
                saved = manifest_path.parent / record['backupPath']
                self.assertEqual(saved.read_bytes(), path.read_bytes())
                self.assertEqual(digest(saved.read_bytes()), record['sha256'])
                observed.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', new=unlink):
            ra.apply_raw_ingest_plan(plan, move_source=True)
        self.assertEqual(observed, [future])
        self.assertFalse(first.exists() or second.exists())

    def test_future_backup_failure_preserves_both_sources_and_published_object(self):
        plan, first, second = self.future_pair()
        original = ra._atomic_bytes
        def fail_backup(path, data):
            if 'raw-folded' in path.parts:
                raise OSError('synthetic backup failure')
            return original(path, data)
        with patch.object(ra, '_atomic_bytes', side_effect=fail_backup), self.assertRaisesRegex(OSError, 'backup failure'):
            ra.apply_raw_ingest_plan(plan)
        self.assertEqual((plan.archive / first.name).read_bytes(), first.read_bytes())
        self.assertTrue(first.exists() and second.exists())
        self.assertFalse((plan.archive / second.name).exists())

    def test_future_plan_reordering_or_proof_tampering_is_rejected(self):
        for mutation in ('order', 'proof', 'dependency'):
            with self.subTest(mutation=mutation):
                plan, first, second = self.future_pair()
                last = plan.actions[-1]
                if mutation == 'order':
                    plan.actions.reverse()
                elif mutation == 'proof':
                    last.projected_objects[plan.archive / first.name] = (second, last.source_sha)
                else:
                    last.ordered_after = ()
                with self.assertRaisesRegex(ValueError, 'after final'):
                    ra.apply_raw_ingest_plan(plan)
                self.assertFalse(list(plan.archive.iterdir()))

    def test_late_range_predecessor_refusal_blocks_only_dependent_chain(self):
        plan, first, second = self.future_pair(policy='daily')
        other, _ = self.fixture.snapshot(self.fixture.root / 'independent', accounts=['wxid_independent'])
        other_bytes = other.read_bytes()
        plan = ra.plan_raw_ingest(plan.archive, [first.parent, second.parent, other.parent], policy='daily')
        plan.actions[0].block('synthetic late refusal', 'hardBlocked')
        result = ra.apply_raw_ingest_plan(plan, move_source=True)
        self.assertEqual(set(result.retained), {first, second})
        self.assertIn('range_predecessor_blocked', plan.actions[1].reasons)
        self.assertEqual([p.name for p in plan.archive.iterdir()], [other.name])
        self.assertEqual((plan.archive / other.name).read_bytes(), other_bytes)

    def test_range_archive_only_media_drift_is_rejected(self):
        archive = self.fixture.root / 'archived/raw'
        archive.mkdir(parents=True)
        source, doc = self.fixture.snapshot(self.fixture.root / 'incoming')
        ref = self.fixture.media(archive)
        doc['posts'][0]['media'][0]['localPath'] = ref
        self.fixture.save(source, doc)
        plan = ra.plan_raw_ingest(archive, [source.parent])
        self.assertTrue(all(a.status == 'ready' for a in plan.actions))
        (archive / ref).write_bytes(b'changed after planning')
        before = tree(archive)
        with self.assertRaisesRegex(ValueError, 'range media changed'):
            ra.apply_raw_ingest_plan(plan)
        self.assertEqual(tree(archive), before)
        self.assertTrue(source.is_file())

    def test_finalized_kind_cannot_move_json_ahead_of_its_media(self):
        archive = self.fixture.root / 'archived/raw'
        archive.mkdir(parents=True)
        source, doc = self.fixture.snapshot(self.fixture.root / 'first')
        media_root = self.fixture.root / 'second'
        ref = self.fixture.media(media_root)
        doc['posts'][0]['media'][0]['localPath'] = ref
        self.fixture.save(source, doc)
        plan = ra.plan_raw_ingest(archive, [source.parent, media_root])
        action = next(a for a in plan.actions if a.source == source)
        action.kind = 'file'
        with self.assertRaisesRegex(ValueError, 'after final'):
            ra.apply_raw_ingest_plan(plan)
        self.assertFalse(list(archive.iterdir()))

    @unittest.skipUnless(os.name == 'nt', 'requires Windows case-insensitive paths')
    def test_range_case_alias_is_occupied_then_visible_to_later_retirement(self):
        config = range_fixtures._config(self.fixture.root)
        archive = range_fixtures.load_config(config).paths.archived / 'raw'
        old, _ = self.fixture.snapshot(archive, day=date(2010, 1, 1))
        upper_name = old.stem[:-8] + old.stem[-8:].upper() + old.suffix
        old.rename(old.with_name(upper_name))
        self.assertTrue(old.samefile(old.with_name(upper_name)))
        first, _ = self.fixture.snapshot(self.fixture.root / 'first', ids=('first', 'second'), day=date(2010, 1, 1))
        second, _ = self.fixture.snapshot(self.fixture.root / 'second', ids=('first', 'second', 'third'))
        expected = self.fixture.root / 'legacy-case-oracle'
        expected.mkdir()
        (expected / upper_name).write_bytes(old.read_bytes())
        with redirect_stdout(io.StringIO()):
            for source in (first, second):
                range_fixtures.ranges.publish_range(source, expected / source.name)
        plan = ra.plan_raw_ingest(archive, [first.parent, second.parent])
        self.assertIn(archive / first.name, plan.actions[1].retire)
        with redirect_stdout(io.StringIO()):
            code = range_fixtures.archive_exports.main(['--config', str(config),
                '--raw-root', str(first.parent), '--raw-root', str(second.parent)])
        self.assertEqual(code, 0)
        snapshot = lambda root: {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
        self.assertEqual(snapshot(archive), snapshot(expected))

    def test_backup_session_directory_cannot_occupy_ledger_namespace(self):
        for number, directory in enumerate(('ordinary-session', 'manifest.json', 'MANIFEST.JSON')):
            with self.subTest(directory=directory):
                root = self.fixture.root / str(number)
                archive, incoming = root / 'archived/raw', root / 'incoming'
                old = save(archive / directory / 'Old_20300102.json', payload((1,)))
                source = save(incoming / 'Current_20300102/Current_20300102.json', payload((1, 2)))
                before, incoming_bytes = old.read_bytes(), source.read_bytes()
                plan = ra.plan_raw_ingest(archive, [incoming])
                action, = plan.actions
                self.assertEqual(action.status, 'ready')
                self.assertIn(old, action.retire)
                result = ra.apply_raw_ingest_plan(plan, move_source=True)
                ledger = result.backup_dir / 'manifest.json'
                entry, = json.loads(ledger.read_bytes())['items']
                saved = result.backup_dir / entry['backupPath']
                self.assertEqual(saved.read_bytes(), before)
                self.assertEqual(digest(saved.read_bytes()), entry['sha256'])
                self.assertFalse(old.exists() or source.exists())
                self.assertEqual(action.target.read_bytes(), incoming_bytes)

    def test_backup_ledger_cannot_overwrite_a_raw_manifest_file(self):
        old, _ = self.fixture.snapshot(self.fixture.root / 'archived/raw')
        source, _ = self.fixture.snapshot(self.fixture.root / 'incoming', ids=('first', 'second'))
        old = old.rename(old.with_name('manifest.json'))
        source = source.rename(source.with_name('manifest.json'))
        before = old.read_bytes()
        result = ra.apply_raw_ingest_plan(ra.plan_raw_ingest(old.parent, [source.parent]), force_overwrite=True)
        ledger = result.backup_dir / 'manifest.json'
        entry, = json.loads(ledger.read_bytes())['items']
        saved = result.backup_dir / entry['backupPath']
        self.assertNotEqual(saved, ledger)
        self.assertEqual(saved.read_bytes(), before)
        self.assertEqual(digest(saved.read_bytes()), entry['sha256'])

    def backup_namespace_case(self, *, prefix_file):
        archive = self.fixture.root / 'archived/raw'
        old, _ = self.fixture.snapshot(archive, day=date(2010, 1, 1))
        first, _ = self.fixture.snapshot(self.fixture.root / 'first', ids=('first', 'second'), day=date(2010, 1, 1))
        second, _ = self.fixture.snapshot(self.fixture.root / 'second', ids=('first', 'second', 'third'))
        versions = {digest(old.read_bytes()): old.read_bytes(), digest(first.read_bytes()): first.read_bytes()}
        if prefix_file:
            reserved = archive / '_versions'
            reserved.write_bytes(b'old ordinary root file')
            (first.parent / '_versions').write_bytes(b'new ordinary root file')
        else:
            # A legitimate raw-relative path can also occupy the generated
            # version slot; it must keep its own, different original bytes.
            relative = Path('_versions') / digest(first.read_bytes()) / first.name
            reserved, _ = self.fixture.snapshot(archive / relative.parent, ids=('nested',), day=date(2010, 1, 1))
            self.fixture.snapshot(first.parent / relative.parent, ids=('nested', 'new'), day=date(2010, 1, 1))
        versions[digest(reserved.read_bytes())] = reserved.read_bytes()
        plan = ra.plan_raw_ingest(archive, [first.parent, second.parent])
        self.assertTrue(all(a.status == 'ready' for a in plan.actions))
        result = ra.apply_raw_ingest_plan(plan, force_overwrite=True)
        entries = json.loads((result.backup_dir / 'manifest.json').read_bytes())['items']
        for sha, data in versions.items():
            entry, = [p for p in entries if p['sha256'] == sha]
            saved = result.backup_dir / entry['backupPath']
            self.assertEqual(saved.read_bytes(), data)
            self.assertEqual(digest(saved.read_bytes()), sha)
        self.assertFalse(old.exists())
        self.assertEqual((archive / second.name).read_bytes(), second.read_bytes())

    def test_backup_version_prefix_can_itself_be_an_original_file(self):
        self.backup_namespace_case(prefix_file=True)

    def test_backup_version_slot_cannot_replace_another_original_backup(self):
        self.backup_namespace_case(prefix_file=False)

    def test_range_membership_drift_is_rejected_before_any_publication(self):
        plan, first, second = self.future_pair()
        other, _ = self.fixture.snapshot(plan.archive, day=date(2014, 1, 1))
        before = tree(plan.archive)
        with self.assertRaisesRegex(ValueError, 'range archive changed'):
            ra.apply_raw_ingest_plan(plan)
        self.assertEqual(tree(plan.archive), before)
        self.assertTrue(first.exists() and second.exists() and other.exists())

    def test_retention_target_is_planned_once_and_apply_is_the_only_writer(self):
        old, _ = self.fixture.snapshot(self.fixture.root / 'archived/raw', ids=('first', 'missing'))
        new, _ = self.fixture.snapshot(self.fixture.root / 'incoming', ids=('first',))
        before = old.read_bytes()
        plan = ra.plan_raw_ingest(old.parent, [new.parent])
        action, = plan.actions
        self.assertNotEqual(action.target, old)
        self.assertIn('_保留_', action.target.name)
        self.assertEqual(action.retire, [])
        with patch('wechat_diary_core.moments_ranges.publish_range', side_effect=AssertionError('archive bypass')):
            result = ra.apply_raw_ingest_plan(plan)
        self.assertEqual(result.published, [action.target])
        self.assertEqual(action.target.read_bytes(), new.read_bytes())
        self.assertEqual(old.read_bytes(), before)
        self.assertTrue(result.range_review)

    def test_retired_range_is_in_plan_and_backup_is_readable_before_removal(self):
        old, _ = self.fixture.snapshot(self.fixture.root / 'archived/raw', ids=('first',), day=date(2010, 1, 1))
        new, _ = self.fixture.snapshot(self.fixture.root / 'incoming', ids=('first', 'second'))
        before = old.read_bytes()
        plan = ra.plan_raw_ingest(old.parent, [new.parent])
        action, = plan.actions
        self.assertEqual(action.retire, [old])
        original = Path.unlink
        events = []
        def unlink(path, *args, **kwargs):
            if path == old:
                copies = list((old.parent.parent / 'raw-folded').glob('*/' + old.name))
                self.assertEqual(len(copies), 1)
                self.assertEqual(copies[0].read_bytes(), before)
                events.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', new=unlink):
            result = ra.apply_raw_ingest_plan(plan)
        self.assertEqual(events, [old])
        self.assertEqual(result.retired, [old])
        self.assertEqual(action.target.read_bytes(), new.read_bytes())

    def test_final_check_does_not_consult_route_kind_or_group(self):
        import ast
        import inspect
        tree_ = ast.parse(inspect.getsource(ra._final_target_check))
        forbidden = [node.attr for node in ast.walk(tree_) if isinstance(node, ast.Attribute)
                     and node.attr in ('kind', 'resolution', 'group')]
        self.assertEqual(forbidden, [])

    def test_archive_cli_mutations_are_inside_apply_and_in_its_final_plan(self):
        script = range_fixtures.archive_exports
        config_path = range_fixtures._config(self.fixture.root)
        archive = range_fixtures.load_config(config_path).paths.archived / 'raw'
        self.fixture.snapshot(archive, ids=('first', 'missing'))
        new, _ = self.fixture.snapshot(self.fixture.root / 'incoming', ids=('first',))
        original_apply, original_replace, original_unlink = script.apply_raw_ingest_plan, os.replace, Path.unlink
        active, seen = False, []
        targets, retire = set(), set()

        def apply(plan, **kwargs):
            nonlocal active
            targets.update(a.target for a in plan.actions if a.status == 'ready')
            retire.update(p for a in plan.actions if a.status == 'ready' for p in a.retire)
            active = True
            try:
                return original_apply(plan, **kwargs)
            finally:
                active = False

        def replace(source, target):
            target = Path(target)
            if target.is_relative_to(archive):
                self.assertTrue(active, 'archive write bypassed apply')
                self.assertIn(target, targets, 'archive target not in final plan')
                seen.append(target)
            return original_replace(source, target)

        def unlink(path, *args, **kwargs):
            if path.is_relative_to(archive) and path.exists():
                self.assertTrue(active, 'archive deletion bypassed apply')
                self.assertIn(path, retire, 'archive deletion not in final plan')
            return original_unlink(path, *args, **kwargs)

        with patch.object(script, 'apply_raw_ingest_plan', new=apply), patch.object(os, 'replace', new=replace), \
             patch.object(Path, 'unlink', new=unlink), redirect_stdout(io.StringIO()):
            self.assertEqual(script.main(['--config', str(config_path), '--raw-root', str(new.parent)]), 3)
        self.assertEqual(len(seen), 1)

    def test_archive_retirement_failure_keeps_source_and_returns_review(self):
        script = range_fixtures.archive_exports
        config_path = range_fixtures._config(self.fixture.root)
        archive = range_fixtures.load_config(config_path).paths.archived / 'raw'
        old, _ = self.fixture.snapshot(archive, ids=('first',), day=date(2010, 1, 1))
        new, _ = self.fixture.snapshot(self.fixture.root / 'incoming', ids=('first', 'second'))
        original = Path.unlink
        def fail_old(path, *args, **kwargs):
            if path == old:
                raise PermissionError('synthetic locked old range')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', new=fail_old), redirect_stdout(io.StringIO()):
            code = script.main(['--config', str(config_path), '--raw-root', str(new.parent), '--move-source'])
        self.assertEqual(code, 3)
        self.assertTrue(old.exists() and new.exists())
        self.assertEqual((archive / new.name).read_bytes(), new.read_bytes())
        backup, = (archive.parent / 'raw-folded').glob('*/' + old.name)
        self.assertEqual(backup.read_bytes(), old.read_bytes())

    def test_duplicate_publication_reuses_same_target_and_original_backup(self):
        old, _ = self.fixture.snapshot(self.fixture.root / 'archived/raw', ids=('first',))
        source, _ = self.fixture.snapshot(self.fixture.root / 'incoming', ids=('first', 'second'))
        other = self.fixture.root / 'second' / source.name
        other.parent.mkdir()
        other.write_bytes(source.read_bytes())
        original_bytes, incoming_bytes = old.read_bytes(), source.read_bytes()
        plan = ra.plan_raw_ingest(old.parent, [source.parent, other.parent])
        self.assertTrue(all(a.status == 'ready' for a in plan.actions))
        writes = []
        original_replace = os.replace
        def replace(src, dst):
            if Path(dst) == old:
                writes.append(Path(dst))
            return original_replace(src, dst)
        with patch.object(os, 'replace', new=replace):
            result = ra.apply_raw_ingest_plan(plan, move_source=True)
        self.assertEqual(writes, [old])
        self.assertEqual(old.read_bytes(), incoming_bytes)
        self.assertEqual(set(result.consumed), {source, other})
        self.assertFalse(source.exists() or other.exists())
        manifest = json.loads((result.backup_dir / 'manifest.json').read_bytes())
        self.assertEqual(len(manifest['items']), 1)
        self.assertEqual((result.backup_dir / old.name).read_bytes(), original_bytes)
