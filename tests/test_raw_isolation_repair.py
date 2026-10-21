"""Range isolation regressions and per-file failures; only synthetic temp inputs.

The CAS/range shapes are copied and adapted from the approved adversarial probe.
"""
from contextlib import ExitStack, redirect_stdout
from datetime import date
import copy
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_moments_ranges as fixtures
from test_raw_archive import payload, save
from wechat_diary_core import raw_archive as raw, raw_isolation as isolation


def cas(root, content, *, name_content=None):
    digest = hashlib.md5(content if name_content is None else name_content).hexdigest()
    relative = f"_media/images/{digest[:2]}/{digest}.jpg"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return relative


class RawIsolationRepairTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MomentsRangeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def roots(self, name):
        archive, live = self.root / name / 'archived/raw', self.root / name / 'live'
        archive.mkdir(parents=True)
        live.mkdir()
        return archive, live

    def snapshot(self, root, references, *, end=date(2026, 8, 5)):
        path, data = self.fixture.snapshot(root, end=end)
        post = data['posts'][0]
        template = copy.deepcopy(post['media'][0])
        post['media'] = []
        for reference in references:
            item = copy.deepcopy(template)
            item['localPath'] = reference
            item.pop('livePhoto', None)
            post['media'].append(item)
        self.fixture.save(path, data)
        return path

    def execute(self, archive, live, patches=()):
        plan = raw.plan_raw_ingest(archive, [live], policy='daily')
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(io.StringIO()))
            for guard in patches:
                stack.enter_context(guard)
            result = raw.apply_raw_ingest_plan(plan, move_source=True)
            raw.report_daily_result(plan, result)
        return plan, result

    def assert_copied(self, live, source, result, original):
        target = result.isolation_dir / source.relative_to(live)
        self.assertEqual(target.read_bytes(), original[source])
        doc = raw._json_prefix_for_media(target.read_bytes())
        for ref in raw._chat_cas_references(doc, target, result.isolation_dir):
            old = live / ref.relative_to(result.isolation_dir)
            if old in original:  # An input-dangling reference is not an isolation failure.
                self.assertEqual(ref.read_bytes(), original[old])
        self.assertFalse(source.exists())

    def test_blocked_ranges_preserve_existing_cas_for_all_three_failure_shapes(self):
        for mode in ('schema', 'md5', 'missing'):
            with self.subTest(mode=mode):
                archive, live = self.roots(mode)
                refs = [cas(live, b'good range media')]
                if mode == 'md5':
                    refs.append(cas(live, b'truncated', name_content=b'full original'))
                if mode == 'missing':
                    refs.append('_media/images/00/' + '0' * 32 + '.jpg')
                source = self.snapshot(live, refs)
                if mode == 'schema':
                    data = json.loads(source.read_bytes())
                    data['posts'][0].pop('createTime')
                    self.fixture.save(source, data)
                original = {p: p.read_bytes() for p in live.rglob('*') if p.is_file()}
                plan, result = self.execute(archive, live)
                self.assertNotEqual(next(a.status for a in plan.actions if a.source == source), 'ready')
                self.assert_copied(live, source, result, original)
                if mode == 'missing':
                    self.assertFalse((result.isolation_dir / refs[-1]).exists())

    def test_apply_retirement_failure_preserves_cas_and_reports_actual_reason(self):
        archive, live = self.roots('retire')
        old = self.snapshot(archive, [cas(archive, b'shared range media')])
        source = self.snapshot(live, [cas(live, b'shared range media')], end=date(2026, 1, 1))
        original = {p: p.read_bytes() for p in live.rglob('*') if p.is_file()}
        unlink = raw._unlink
        def locked(path):
            if path == old:
                raise PermissionError('synthetic old range lock')
            unlink(path)
        _, result = self.execute(archive, live, [patch.object(raw, '_unlink', side_effect=locked)])
        self.assert_copied(live, source, result, original)
        self.assertTrue(old.is_file())
        report = archive.parent / 'raw-fold-report.json'
        item = next(i for i in json.loads(report.read_bytes())['items']
                    if 'range_retirement_failed' in i['reasons'])
        self.assertEqual(item['source'], str(result.isolated_sources[source]))
        md = report.with_suffix('.md').read_text(encoding='utf-8')
        self.assertIn('range_retirement_failed', md)
        self.assertNotIn('raw 已无待处理项', md)
        manifest = json.loads(result.isolation_dir.with_suffix('.manifest.json').read_bytes())
        row = next(x for x in manifest['files'] if x['source'] == str(source))
        self.assertIn('range_retirement_failed', row['reasons'])

    def test_successful_range_consumes_deferred_cas_without_isolation(self):
        archive, live = self.roots('ready')
        self.snapshot(live, [cas(live, b'ready media')])
        _, result = self.execute(archive, live)
        self.assertFalse(result.retained)
        self.assertEqual([p for p in live.rglob('*') if p.is_file()], [])
        self.assertFalse(isolation.retention_root(archive.parent).exists())

    def test_archive_fallback_requires_md5_even_when_original_sha_matches(self):
        for valid in (True, False):
            with self.subTest(valid=valid):
                archive, live = self.roots('fallback-' + str(valid))
                ref = cas(live, b'original bytes', name_content=None if valid else b'different name bytes')
                source = self.snapshot(live, [ref])
                data = json.loads(source.read_bytes())
                data['posts'][0].pop('createTime')
                self.fixture.save(source, data)
                original = {p: p.read_bytes() for p in live.rglob('*') if p.is_file()}
                real_isolate = isolation.isolate_retained
                def already_published(plan, result):
                    self.assertEqual((archive / ref).read_bytes(), original[live / ref])
                    (live / ref).unlink()
                    return real_isolate(plan, result)
                _, result = self.execute(archive, live, [patch.object(isolation, 'isolate_retained', side_effect=already_published)])
                if valid:
                    self.assert_copied(live, source, result, original)
                else:
                    self.assertEqual(source.read_bytes(), original[source])
                    self.assertFalse((result.isolation_dir / source.name).exists())
                    report = json.loads((archive.parent / 'raw-fold-report.json').read_bytes())
                    self.assertTrue(any('isolation_failed:ValueError' in i['reasons'] for i in report['items']))

    def test_encoding_failure_is_reported_without_aborting_other_files(self):
        archive, live = self.roots('encoding')
        sources = []
        for name in ('Broken\ud83dNick', 'Healthy'):
            save(archive / name / f'{name}_20300102.json', payload(wxid='wxid_old'))
            sources.append(save(live / f'{name}_20300102' / f'{name}_20300102.json', payload(wxid='wxid_new')))
        broken, healthy = sources
        original = broken.read_bytes()
        _, result = self.execute(archive, live)
        self.assertEqual(broken.read_bytes(), original)
        self.assertFalse(healthy.exists())
        self.assertTrue(result.isolated_sources[healthy].is_file())
        self.assertFalse((result.isolation_dir / broken.relative_to(live)).exists())
        report = archive.parent / 'raw-fold-report.json'
        rows = json.loads(report.read_text(encoding='utf-8'))['items']
        item = next(i for i in rows if str(broken) in i.get('sources', [i.get('source')]))
        self.assertIn('isolation_failed:UnicodeEncodeError', item['reasons'])
        self.assertIn('isolation_failed:UnicodeEncodeError', report.with_suffix('.md').read_text(encoding='utf-8'))

    def test_value_errors_in_copy_manifest_and_unlink_are_handled(self):
        for phase in ('copy', 'manifest', 'unlink'):
            with self.subTest(phase=phase):
                archive, live = self.roots(phase)
                save(archive / 'Alpha/Alpha_20300102.json', payload(wxid='wxid_old'))
                source = save(live / 'Alpha_20300102/Alpha_20300102.json', payload(wxid='wxid_new'))
                original = source.read_bytes()
                atomic, unlink = raw._atomic_bytes, raw._unlink
                def fail_write(path, data):
                    if '-retained' in str(path) and ((phase == 'manifest') == path.name.endswith('.manifest.json')):
                        raise ValueError('synthetic encoding/value failure')
                    return atomic(path, data)
                def fail_unlink(path):
                    if path == source:
                        raise ValueError('synthetic removal value failure')
                    return unlink(path)
                guard = patch.object(raw, '_unlink', side_effect=fail_unlink) if phase == 'unlink' else patch.object(raw, '_atomic_bytes', side_effect=fail_write)
                _, result = self.execute(archive, live, [guard])
                self.assertEqual(source.read_bytes(), original)
                self.assertNotIn(source, result.isolated_sources)
                report = json.loads((archive.parent / 'raw-fold-report.json').read_bytes())
                self.assertTrue(any('isolation_failed:ValueError' in i['reasons'] for i in report['items']))

    def test_nonempty_isolation_without_items_is_not_reported_as_clear(self):
        archive, _ = self.roots('empty-report')
        directory = isolation.retention_root(archive.parent) / '20300103-010203'
        directory.mkdir(parents=True)
        (directory / 'preserved.bin').write_bytes(b'preserved input')
        with redirect_stdout(io.StringIO()):
            isolation.report_without_raw(archive.parent)
        markdown = (archive.parent / 'raw-fold-report.md').read_text(encoding='utf-8')
        self.assertNotIn('raw 已无待处理项', markdown)
        self.assertIn('raw 仍有隔离批次', markdown)

    def test_dependency_collection_failure_keeps_ready_and_blocked_sources_live(self):
        for ready in (True, False):
            with self.subTest(ready=ready):
                archive, live = self.roots('prepare-' + str(ready))
                ref = cas(live, b'preserved dependency')
                data = payload(wxid='wxid_new')
                data['messages'][0]['source'] = '../' + ref
                source = save(live / 'Alpha_20300102/Alpha_20300102.json', data)
                if not ready:
                    save(archive / 'Alpha/Alpha_20300102.json', payload(wxid='wxid_old'))
                healthy = save(live / 'Healthy_20300103/Healthy_20300103.json', payload(wxid='wxid_healthy'))
                original = source.read_bytes()
                plan = raw.plan_raw_ingest(archive, [live], policy='daily')
                before = plan.to_dict()
                collect = raw._chat_cas_references
                def failed(payload, path, root):
                    if path == source:
                        raise ValueError('synthetic per-file dependency error')
                    return collect(payload, path, root)
                with patch.object(raw, '_chat_cas_references', side_effect=failed), redirect_stdout(io.StringIO()):
                    result = raw.apply_raw_ingest_plan(plan, move_source=True)
                    raw.report_daily_result(plan, result)
                self.assertEqual(plan.to_dict(), before)
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual((live / ref).read_bytes(), b'preserved dependency')
                self.assertNotIn(source, result.isolated_sources)
                self.assertFalse(healthy.exists())
                report = json.loads((archive.parent / 'raw-fold-report.json').read_bytes())
                self.assertTrue(any('isolation_failed:ValueError' in i['reasons'] and
                                    str(source) in i.get('sources', [i.get('source')]) for i in report['items']))

    def test_dependency_failure_does_not_break_the_planned_range_publication_chain(self):
        archive, live = self.roots('prepare-chain')
        self.snapshot(archive, [cas(archive, b'range chain media')])
        reference = cas(live, b'range chain media')
        first = self.snapshot(live, [reference], end=date(2026, 1, 1))
        second = self.snapshot(live, [reference], end=date(2027, 1, 1))
        original = first.read_bytes()
        plan = raw.plan_raw_ingest(archive, [live], policy='daily')
        self.assertTrue(all(a.status == 'ready' for a in plan.actions))
        collect = raw._chat_cas_references
        def failed(payload, path, root):
            if path == first:
                raise ValueError('synthetic first range dependency failure')
            return collect(payload, path, root)
        with patch.object(raw, '_chat_cas_references', side_effect=failed), redirect_stdout(io.StringIO()):
            result = raw.apply_raw_ingest_plan(plan, move_source=True)
            raw.report_daily_result(plan, result)
        self.assertEqual(first.read_bytes(), original)
        self.assertNotIn(first, result.isolated_sources)
        self.assertFalse(second.exists())
        self.assertTrue((archive / second.name).is_file())
        self.assertEqual((live / reference).read_bytes(), b'range chain media')
        rows = json.loads((archive.parent / 'raw-fold-report.json').read_bytes())['items']
        self.assertTrue(any(i.get('source') == str(first) and 'isolation_failed:ValueError' in i['reasons'] for i in rows))
