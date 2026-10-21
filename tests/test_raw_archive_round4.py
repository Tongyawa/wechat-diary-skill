"""Guard the fallback destination and make each retained item actionable."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_raw_archive import payload, save
from wechat_diary_core import raw_archive as ra
from wechat_diary_core.backends.weflow_api.backend import _historical_export_wxids, _last_known_group_members
from wechat_diary_core.workspace import merge_raw_exports_into_archive, rotate_export_workspace


class RoundFourArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.archive = self.root / 'archived/raw'
        self.archive.mkdir(parents=True)
        self.live = self.root / 'live'
        self.live.mkdir()

    def chat(self, root, name, ids=(1, 2), wxid='wxid_incoming', generation='cas'):
        return save(root / name, payload(ids=ids, wxid=wxid, generation=generation))

    def execute(self, opener=None):
        plan = ra.plan_raw_ingest(self.archive, [self.live], policy='daily')
        result = ra.apply_raw_ingest_plan(plan, move_source=True, include_ranges=True)
        with redirect_stdout(io.StringIO()):
            if opener is None:
                ra.report_daily_result(plan, result)
            else:
                ra.report_daily_result(plan, result, report_opener=opener)
        return plan, result

    def report(self):
        return json.loads((self.archive.parent / 'raw-fold-report.json').read_bytes())

    def test_uncovered_fallback_target_is_retained_with_only_referenced_cas(self):
        victim = self.chat(self.archive, 'A/Former_20300101.json', (1, 2, 3))
        before = victim.read_bytes()
        doc = payload(ids=(1, 2), wxid='wxid_incoming')
        doc['messages'][0]['source'] = '../_media/images/ab/' + 'ab' * 16 + '.jpg'
        source = save(self.live / 'A_20300101/Former_20300101.json', doc)
        needed = self.live / '_media/images/ab' / ('ab' * 16 + '.jpg')
        needed.parent.mkdir(parents=True)
        needed.write_bytes(b'needed')
        unused = needed.with_name('unused.jpg')
        unused.write_bytes(b'unused')
        for iteration in range(2):
            plan, result = self.execute()
            self.assertEqual(victim.read_bytes(), before)
            self.assertTrue(source.exists())
            self.assertTrue(needed.exists())
            self.assertFalse(unused.exists())
            self.assertTrue(any(a.status != 'ready' for a in plan.actions if a.kind == 'chat'))
        self.assertEqual((self.archive / unused.relative_to(self.live)).read_bytes(), b'unused')

    def test_other_identity_never_replaced_and_victim_key_is_reported(self):
        for reason in ('directory', 'coverage', 'planned_directory'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                archive, live = Path(temporary) / 'archived/raw', Path(temporary) / 'live'
                victim = self.chat(archive, 'A/A_20300101.json', (7, 8, 9), 'wxid_victim')
                before = victim.read_bytes()
                if reason == 'coverage':
                    self.chat(archive, 'Old/Old_20300101.json', (1, 2, 3))
                if reason == 'planned_directory':
                    # Both individually resolve to Old; settling the planned directory
                    # forces the first chat back to an occupied A destination.
                    self.chat(archive, 'Old/Old_20291231.json')
                    self.chat(live, 'Old_20300102/Old_20300102.json', (8,), 'wxid_second')
                source = self.chat(live, 'A_20300101/A_20300101.json', (1,))
                with redirect_stdout(io.StringIO()):
                    merge_raw_exports_into_archive(live, archive)
                self.assertEqual(victim.read_bytes(), before)
                self.assertTrue(source.exists())
                report = json.loads((archive.parent / 'raw-fold-report.json').read_bytes())
                self.assertIn('wxid_victim', json.dumps(report))
                self.assertIn('2030-01-01', json.dumps(report))

    def test_bad_archive_is_backed_up_and_reread_before_repair(self):
        victim = self.chat(self.archive, 'A/A_20300101.json')
        victim.write_bytes(victim.read_bytes()[:-15])
        before = victim.read_bytes()
        source = self.chat(self.live, 'A_20300101/A_20300101.json', (1, 2, 3))
        expected = source.read_bytes()
        original = os.replace
        observed = []
        def replace(src, dst):
            if Path(dst) == victim:
                copies = list((self.archive.parent / 'raw-folded').glob('*/A/A_20300101.json'))
                self.assertEqual(len(copies), 1)
                self.assertEqual(copies[0].read_bytes(), before)
                observed.append(True)
            return original(src, dst)
        with patch.object(os, 'replace', side_effect=replace):
            self.execute()
        self.assertEqual(observed, [True])
        self.assertEqual(victim.read_bytes(), expected)
        manifest, = (self.archive.parent / 'raw-folded').glob('*/manifest.json')
        record = json.loads(manifest.read_bytes())['items'][0]
        self.assertEqual(record['backupBasis'], 'archive_unreadable')
        self.assertFalse(record['coverage']['covered'])

    def test_bad_incoming_cannot_overwrite_chat_but_absent_target_keeps_old_behavior(self):
        forms = [b'{"session": {', b'{"posts": []}', b'[]',
                 json.dumps(dict(payload(), messages=None)).encode(),
                 json.dumps(dict(payload(), messages=[{}])).encode()]
        for raw in forms:
            with self.subTest(raw=raw[:25]), tempfile.TemporaryDirectory() as temporary:
                archive, live = Path(temporary) / 'archived/raw', Path(temporary) / 'live'
                old = self.chat(archive, 'A/A_20300101.json', wxid='wxid_victim')
                before = old.read_bytes()
                source = self.chat(live, 'A_20300101/A_20300101.json')
                source.write_bytes(raw)
                with redirect_stdout(io.StringIO()):
                    merge_raw_exports_into_archive(live, archive)
                self.assertEqual(old.read_bytes(), before)
                self.assertEqual(source.read_bytes(), raw)
                report = json.loads((archive.parent / 'raw-fold-report.json').read_bytes())
                self.assertIn('invalid_incoming_chat', json.dumps(report))
                # Empty destinations retain the historical byte-preserving behavior.
                empty = Path(temporary) / 'empty/raw'
                with redirect_stdout(io.StringIO()):
                    merge_raw_exports_into_archive(live, empty)
                self.assertEqual((empty / 'A/A_20300101.json').read_bytes(), raw)

    @unittest.skipUnless(os.name == 'nt', 'Windows path semantics')
    def test_case_matching_is_normal_daily_resolution_and_bulk_stays_strict(self):
        old = self.chat(self.archive, 'Abc/Abc_20300101.json', (1, 2, 3))
        self.chat(self.archive, 'Other/Other_20300102.json')
        source = self.chat(self.live, 'ABC_20300101/ABC_20300101.json', (1,))
        bulk = ra.plan_raw_ingest(self.archive, [self.live])
        self.assertNotEqual(bulk.actions[0].status, 'ready')
        plan = ra.plan_raw_ingest(self.archive, [self.live], policy='daily')
        self.assertEqual(plan.actions[0].target, old)
        self.assertNotEqual(plan.actions[0].status, 'ready')
        expected = old.read_bytes()
        self.execute()
        self.assertEqual(old.read_bytes(), expected)
        self.assertTrue(source.exists())

    def test_fallback_coverage_uses_bom_and_cross_lineage_identities(self):
        for generation in ('cas', 'legacy'):
            for bom in (False, True):
                for covered in (False, True):
                    with self.subTest(generation=generation, bom=bom, covered=covered), tempfile.TemporaryDirectory() as temporary:
                        archive, live = Path(temporary) / 'archived/raw', Path(temporary) / 'live'
                        old = self.chat(archive, 'A/Former_20300101.json', (1, 2, 3), generation=generation)
                        # Another wxid in the directory triggers resolution fallback.
                        self.chat(archive, 'A/Other_20300102.json', wxid='wxid_other')
                        if bom:
                            old.write_bytes(b'\xef\xbb\xbf' + old.read_bytes())
                        before = old.read_bytes()
                        source = self.chat(live, 'A_20300101/Former_20300101.json', (1, 2, 3, 4) if covered else (1, 2))
                        expected = source.read_bytes()
                        with redirect_stdout(io.StringIO()):
                            merge_raw_exports_into_archive(live, archive)
                        self.assertEqual(old.read_bytes(), expected if covered else before)
                        self.assertEqual(source.exists(), not covered)
                        if covered:
                            self.assertFalse((archive.parent / 'raw-folded').exists())

    def test_bom_history_and_group_roster_remain_readable(self):
        doc = payload(wxid='group_sample@chatroom')
        doc['session']['type'] = '群聊'
        path = save(self.archive / '群聊_Roster/Group_20300101.json', doc)
        plain = _last_known_group_members('group_sample@chatroom', 'Roster', roots=[self.archive])
        self.assertTrue(plain)
        path.write_bytes(b'\xef\xbb\xbf' + path.read_bytes())
        with self.subTest(reader='history'):
            self.assertEqual(_historical_export_wxids(self.archive), ({'group_sample@chatroom'}, True))
        with self.subTest(reader='roster'):
            self.assertEqual(_last_known_group_members('group_sample@chatroom', 'Roster', roots=[self.archive]), plain)
        path.write_bytes(b'not json')
        self.assertFalse(_historical_export_wxids(self.archive)[1])

    def test_report_is_actionable_one_to_one_and_opens_only_when_changed(self):
        self.chat(self.archive, 'Victim/Victim_20300101.json', (5, 6, 7), 'wxid_victim')
        self.chat(self.live, 'Victim_20300101/Victim_20300101.json', (1,))
        opened = Mock()
        self.execute(opened)
        md = self.archive.parent / 'raw-fold-report.md'
        report = self.report()
        text = md.read_text(encoding='utf-8')
        self.assertEqual(text.count('## 待处理 '), len(report['items']))
        opener = text.split('```text\n', 1)[1].split('```', 1)[0]
        for identity in ('wxid_incoming', 'wxid_victim', 'Victim', str(self.root)):
            self.assertNotIn(identity, opener)
        for required in ('raw-fold-report.json', 'raw', '逐项核对', '3 条', '1 条', str(self.live), '每天'):
            self.assertIn(required, text)
        opened.assert_called_once_with(md)
        before = md.read_bytes(), md.stat().st_mtime_ns
        self.execute(opened)
        self.assertEqual((md.read_bytes(), md.stat().st_mtime_ns), before)
        opened.assert_called_once()

    def test_empty_report_neither_creates_markdown_nor_opens(self):
        opened = Mock()
        self.execute(opened)
        self.assertFalse((self.archive.parent / 'raw-fold-report.md').exists())
        opened.assert_not_called()

    def test_resolved_report_keeps_empty_json_and_markdown_without_popup(self):
        self.chat(self.archive, 'A/A_20300101.json', (1, 2, 3))
        self.chat(self.live, 'B_20300101/B_20300101.json', (1,))
        opened = Mock()
        self.execute(opened)
        opened.reset_mock()
        self.chat(self.live, 'A_20300101/A_20300101.json', (1, 2, 3, 4))
        self.execute(opened)
        self.assertEqual(self.report()['items'], [])
        self.assertIn('已无待处理项', (self.archive.parent / 'raw-fold-report.md').read_text(encoding='utf-8'))
        opened.assert_not_called()

    def test_rotation_passes_the_existing_report_opener(self):
        self.chat(self.archive, 'A/Former_20300101.json', (1, 2, 3))
        self.chat(self.live, 'A_20300101/Former_20300101.json', (1,))
        opened = Mock()
        with redirect_stdout(io.StringIO()):
            merge_raw_exports_into_archive(self.live, self.archive, report_opener=opened)
        opened.assert_called_once_with(self.archive.parent / 'raw-fold-report.md')

    def test_truncated_chat_retains_identifiable_message_media_not_metadata(self):
        self.chat(self.archive, 'A/A_20300101.json')
        media = []
        for name in ('ab', 'cd', 'ef'):
            path = self.live / '_media/images' / name / (name * 16 + '.jpg')
            path.parent.mkdir(parents=True)
            path.write_bytes(name.encode())
            media.append(path)
        refs = ['../' + p.relative_to(self.live).as_posix() for p in media]
        doc = payload(wxid='wxid_incoming')
        doc['session']['displayName'] = refs[2]
        doc['messages'][0]['source'] = refs[0]
        doc['messages'][1]['source'] = refs[1]
        source = save(self.live / 'A_20300101/A_20300101.json', doc)
        # Last message has a complete source field but an incomplete later field.
        raw = source.read_bytes()
        end = raw.rfind(b'"isSend"') + 4
        source.write_bytes(raw[:end])
        self.execute()
        self.assertTrue(source.exists())
        self.assertTrue(media[0].exists())
        self.assertTrue(media[1].exists())
        self.assertFalse(media[2].exists())

    def test_repaired_unidentified_incoming_clears_its_old_source_diagnostic(self):
        self.chat(self.archive, 'A/A_20300101.json')
        source = self.chat(self.live, 'A_20300101/A_20300101.json')
        source.write_bytes(b'{"session":')
        self.execute()
        self.assertTrue(self.report()['items'])
        self.chat(self.live, 'A_20300101/A_20300101.json', (1, 2, 3))
        self.execute()
        self.assertEqual(self.report()['items'], [])

    def test_unreadable_target_identity_is_retained_instead_of_assumed_same_chat(self):
        old = self.chat(self.archive, 'A/A_20300101.json')
        old.write_bytes(b'unknown archive object')
        source = self.chat(self.live, 'A_20300101/A_20300101.json')
        self.execute()
        self.assertEqual(old.read_bytes(), b'unknown archive object')
        self.assertTrue(source.exists())
        self.assertIn('final_target_identity_unknown', json.dumps(self.report()))

    def test_report_opener_failure_does_not_undo_publication_or_drop_report(self):
        self.chat(self.archive, 'A/A_20300101.json', (1, 2, 3))
        self.chat(self.live, 'B_20300101/B_20300101.json', (1,))
        self.execute(Mock(side_effect=OSError('synthetic editor failure')))
        self.assertTrue((self.archive / 'B/B_20300101.json').exists())
        self.assertTrue((self.archive.parent / 'raw-fold-report.md').exists())

    def test_real_rotation_callback_multi_item_report_and_opener_negative_controls(self):
        for name in ('First', 'Second'):
            self.chat(self.archive, f'{name}/{name}_20300101.json', (7, 8), 'wxid_victim_' + name)
            self.chat(self.live, f'{name}_20300101/{name}_20300101.json', (1,), 'wxid_incoming_' + name)
        cfg = SimpleNamespace(paths=SimpleNamespace(raw=self.live, processed=self.root / 'processed', archived=self.archive.parent))
        opened = Mock()
        with redirect_stdout(io.StringIO()):
            rotate_export_workspace(cfg, report_opener=opened)
        opened.assert_called_once()
        text = (self.archive.parent / 'raw-fold-report.md').read_text(encoding='utf8')
        items = self.report()['items']
        self.assertEqual(len(items), 2)
        self.assertEqual(text.count('## 待处理 '), 2)
        for item in items:
            self.assertIn(item['identity']['wxid'], text)
            self.assertIn(item['details'][0]['retainedSource'], text)
        opener = text.split('```text\n', 1)[1].split('```', 1)[0]
        def check_generic(candidate):
            for value in ('First', 'Second', 'wxid_incoming_', 'wxid_victim_', str(self.root)):
                self.assertNotIn(value, candidate)
        check_generic(opener)
        for bad in (opener + 'wxid_victim_First', '读 ' + str(self.root) + '/report.md'):
            with self.assertRaises(AssertionError):
                check_generic(bad)
