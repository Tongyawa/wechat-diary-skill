from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts import archive_exports, export_bulk_raw
from wechat_diary_core import moments_ranges as ranges
from wechat_diary_core.backends.weflow_api import mapper
from wechat_diary_core.config import load_config
from test_self_moments_bulk import SELF, _config, _export_fixture, _post


class MomentsRangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stage = self.root / 'stage'
        self.stage.mkdir()
        self.source = _export_fixture(self.stage)
        self.template = self.source['posts'][0]
        self.output = io.StringIO()

    def snapshot(self, root, *, ids=('first',), day=date(2022, 12, 31), end=date(2026, 8, 5), accounts=None):
        root.mkdir(parents=True, exist_ok=True)
        accounts = accounts or [SELF]
        source = copy.deepcopy(self.source)
        source['posts'] = [_post(self.template, identifier, day) for identifier in ids]
        payload = mapper.map_moments_json(source, usernames=[SELF], start=date(2010, 1, 1), end=date(2026, 8, 5))
        payload['filters']['usernames'] = accounts
        for post in payload['posts']:
            post['username'] = accounts[0]
        path = root / mapper.moments_filename(accounts, day, end=end)
        self.save(path, payload)
        return path, payload

    def save(self, path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def media(self, root, content=b'landed image', ext='jpg'):
        digest = hashlib.md5(content).hexdigest()
        path = root / '_media' / ('videos' if ext == 'mov' else 'images') / digest[:2] / f'{digest}.{ext}'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path.relative_to(root).as_posix()

    def publish(self, old, new):
        with redirect_stdout(self.output):
            return ranges.publish_range(new, old.parent / new.name)

    def write(self, raw, posts):
        source = copy.deepcopy(self.source)
        source['posts'] = posts
        with redirect_stdout(self.output), redirect_stderr(io.StringIO()):
            return mapper.write_moments_export(raw, source, usernames=[SELF], start=date(2010, 1, 1), end=date(2026, 8, 5), staging_dir=self.stage)

    def test_actual_local_dates_cross_year_and_empty_name(self):
        posts = [_post(self.template, 'new', date(2026, 1, 1)), _post(self.template, 'old', date(2022, 12, 31))]
        result = self.write(self.root / 'raw', posts)
        self.assertIn('_20221231-20260101_', result.path.name)
        self.assertEqual(result.total_posts, 2)
        empty = self.write(self.root / 'empty', [])
        self.assertRegex(empty.path.name, r'^朋友圈导出_区间_空_[0-9a-f]{8}\.json$')
        self.assertEqual(json.loads(empty.path.read_bytes())['posts'], [])

    def test_complete_superset_retires_legacy_requested_range_and_keeps_daily(self):
        old, _ = self.snapshot(self.root / 'raw', day=date(2010, 1, 1))
        daily = old.parent / mapper.moments_filename([SELF], date(2022, 12, 31))
        daily.write_bytes(b'daily sentinel')
        result = self.write(old.parent, [_post(self.template, 'first', date(2022, 12, 31)), _post(self.template, 'second', date(2026, 8, 5))])
        self.assertFalse(old.exists())
        self.assertTrue(result.path.is_file())
        self.assertFalse(result.replacement_needs_review)
        self.assertEqual(daily.read_bytes(), b'daily sentinel')
        self.assertIn('1 条全部被新文件覆盖', self.output.getvalue())

    def test_missing_post_retained_and_restoring_it_retires_old(self):
        old, data = self.snapshot(self.root / 'raw', ids=('first', 'deleted'))
        original = old.read_bytes()
        result = self.write(old.parent, [_post(self.template, 'first', date(2022, 12, 31))])
        self.assertTrue(result.replacement_needs_review)
        self.assertEqual(old.read_bytes(), original)
        self.assertIn('deleted', self.output.getvalue())
        fixed = self.write(old.parent, [_post(self.template, p['id'], date(2022, 12, 31)) for p in data['posts']])
        self.assertFalse(fixed.replacement_needs_review)
        self.assertFalse(old.exists())

    def test_same_name_regression_publishes_both_then_full_rerun_retires_all(self):
        old, _ = self.snapshot(self.root / 'raw', ids=('first', 'gone'))
        new, data = self.snapshot(self.root / 'incoming')
        before = old.read_bytes()
        result = self.publish(old, new)
        self.assertTrue(result.needs_review)
        self.assertEqual(old.read_bytes(), before)
        self.assertIn('_保留_', result.path.name)
        self.assertIsNotNone(ranges.range_digest(result.path))
        data['posts'].append(copy.deepcopy(data['posts'][0]))
        data['posts'][-1]['id'] = 'gone'
        data['totalPosts'] = 2
        self.save(new, data)
        final = self.publish(old, new)
        self.assertFalse(final.needs_review)
        self.assertEqual(list(old.parent.glob('*.json')), [old])

    def test_missing_media_and_live_photo_each_have_positive_control(self):
        for role in ('media', 'livePhoto'):
            with self.subTest(role=role):
                base = self.root / role
                old, previous = self.snapshot(base / 'raw')
                new, incoming = self.snapshot(base / 'incoming', end=date(2026, 1, 1))
                reference = self.media(old.parent, ext='mov' if role == 'livePhoto' else 'jpg')
                current_reference = self.media(new.parent, ext='mov' if role == 'livePhoto' else 'jpg')
                old_media = previous['posts'][0]['media'][0]
                new_media = incoming['posts'][0]['media'][0]
                target = old_media if role == 'media' else old_media.setdefault('livePhoto', {})
                target['localPath'] = reference
                self.save(old, previous)
                self.assertFalse(ranges.compare_coverage(old, new).covered)
                result = self.publish(old, new)
                self.assertTrue(result.needs_review)
                self.assertTrue(old.exists())
                target = new_media if role == 'media' else new_media.setdefault('livePhoto', {})
                target['localPath'] = current_reference
                self.save(new, incoming)
                self.assertTrue(ranges.compare_coverage(old, new).covered)

    def test_unreadable_new_media_retains_old_then_readable_control_passes(self):
        old, previous = self.snapshot(self.root / 'raw')
        new, incoming = self.snapshot(self.root / 'incoming', end=date(2026, 1, 1))
        reference = self.media(old.parent)
        new_ref = self.media(new.parent)
        previous['posts'][0]['media'][0]['localPath'] = reference
        incoming['posts'][0]['media'][0]['localPath'] = new_ref
        self.save(old, previous); self.save(new, incoming)
        real_open = Path.open
        def deny(path, *args, **kwargs):
            if path == new.parent / new_ref:
                raise PermissionError('injected unreadable media')
            return real_open(path, *args, **kwargs)
        with patch.object(Path, 'open', deny):
            coverage = ranges.compare_coverage(old, new)
            self.assertFalse(coverage.covered)
            self.assertIn('unreadable', str(coverage.reasons))
        self.assertTrue(ranges.compare_coverage(old, new).covered)
        # Actual publication cannot delete while the old bytes are unreadable either.
        (old.parent / reference).unlink()
        incoming['posts'][0]['media'][0]['localPath'] = self.media(old.parent, ext='png')
        self.save(new, incoming)
        self.assertTrue(self.publish(old, new).needs_review)
        self.assertTrue(old.exists())

    def test_corrupt_cas_and_missing_file_fail_then_restored_bytes_pass(self):
        old, previous = self.snapshot(self.root / 'raw')
        new, incoming = self.snapshot(self.root / 'incoming')
        old_ref, new_ref = self.media(old.parent), self.media(new.parent)
        previous['posts'][0]['media'][0]['localPath'] = old_ref
        incoming['posts'][0]['media'][0]['localPath'] = new_ref
        self.save(old, previous); self.save(new, incoming)
        media = new.parent / new_ref
        media.write_bytes(b'damaged bytes')
        self.assertFalse(ranges.compare_coverage(old, new).covered)
        media.unlink()
        self.assertFalse(ranges.compare_coverage(old, new).covered)
        self.media(new.parent)
        self.assertTrue(ranges.compare_coverage(old, new).covered)

    def test_different_digest_never_retires_then_same_digest_does(self):
        old, _ = self.snapshot(self.root / 'raw', accounts=['other_account'])
        new, _ = self.snapshot(self.root / 'incoming')
        self.assertFalse(ranges.compare_coverage(old, new).covered)
        self.assertFalse(self.publish(old, new).needs_review)
        self.assertTrue(old.exists())
        matching, _ = self.snapshot(self.root / 'matching', accounts=['other_account'], end=date(2026, 1, 1))
        self.assertTrue(ranges.compare_coverage(old, matching).covered)
        self.publish(old, matching)
        self.assertFalse(old.exists())

    def test_actual_accounts_must_match_even_if_digest_filename_matches(self):
        old, _ = self.snapshot(self.root / 'raw')
        new, data = self.snapshot(self.root / 'incoming')
        data['filters']['usernames'].append('other_account')
        self.save(new, data)
        self.assertFalse(ranges.compare_coverage(old, new).covered)
        data['filters']['usernames'].pop()
        self.save(new, data)
        self.assertTrue(ranges.compare_coverage(old, new).covered)

    def test_metadata_changes_report_counts_and_do_not_block_same_name_replacement(self):
        old, _ = self.snapshot(self.root / 'raw')
        new, data = self.snapshot(self.root / 'incoming')
        data['posts'][0].update(nickname='Renamed', likes=['Like Placeholder'], comments=[{'id':'comment','nickname':'Commenter','content':'changed','refCommentId':'','refNickname':''}])
        data['exportTime'] = '2026-10-15 10:00:00'
        self.save(new, data)
        result = self.publish(old, new)
        self.assertFalse(result.needs_review)
        self.assertEqual(result.path.read_bytes(), new.read_bytes())
        for field in ('comments', 'likes', 'nickname', 'exportTime'):
            self.assertIn(f'{field} 变化 1', self.output.getvalue())

    def test_failed_final_publication_keeps_all_old_bytes(self):
        old, _ = self.snapshot(self.root / 'raw')
        new, _ = self.snapshot(self.root / 'incoming', end=date(2026, 1, 1))
        before = old.read_bytes()
        with patch.object(ranges.os, 'replace', side_effect=OSError('publish failed')):
            with self.assertRaisesRegex(OSError, 'publish failed'):
                self.publish(old, new)
        self.assertEqual(old.read_bytes(), before)
        self.assertFalse((old.parent / new.name).exists())

    def test_delete_failure_keeps_new_and_reports_review(self):
        old, _ = self.snapshot(self.root / 'raw')
        new, _ = self.snapshot(self.root / 'incoming', end=date(2026, 1, 1))
        original_unlink = Path.unlink
        def fail(path, *args, **kwargs):
            if path == old:
                raise PermissionError('injected locked old file')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail):
            result = self.publish(old, new)
        self.assertTrue(result.needs_review)
        self.assertTrue(old.exists()); self.assertTrue(result.path.exists())
        self.assertIn('删除失败', self.output.getvalue())

    def test_empty_result_preserves_nonempty_old_snapshot(self):
        old, _ = self.snapshot(self.root / 'raw')
        result = self.write(old.parent, [])
        self.assertTrue(result.replacement_needs_review)
        self.assertTrue(old.exists())
        self.assertEqual(json.loads(result.path.read_bytes())['totalPosts'], 0)

    def test_archive_and_export_call_the_same_predicate_with_media_published_first(self):
        config = _config(self.root)
        archive = load_config(config).paths.archived / 'raw'
        old, previous = self.snapshot(archive, day=date(2010, 1, 1))
        raw = self.root / 'incoming'
        first = self.write(raw, [_post(self.template, 'first', date(2022, 12, 31))])
        # First place real CAS references into both snapshots, as an exported fixture would.
        previous['posts'][0]['media'][0]['localPath'] = self.media(archive)
        self.save(old, previous)
        data = json.loads(first.path.read_bytes())
        data['posts'][0]['media'][0]['localPath'] = self.media(raw)
        self.save(first.path, data)
        with patch.object(ranges, 'compare_coverage', wraps=ranges.compare_coverage) as predicate, redirect_stdout(self.output):
            self.assertEqual(archive_exports.main(['--raw-root', str(raw), '--config', str(config)]), 0)
            self.assertGreater(predicate.call_count, 0)
            self.assertFalse(old.exists())
            self.assertTrue((archive / first.path.name).exists())
            self.write(raw, [_post(self.template, 'first', date(2022, 12, 31))])
            self.assertGreater(predicate.call_count, 1)
        self.assertTrue(raw.exists())

    def test_archive_same_name_metadata_changes_need_no_force_and_subset_keeps_source(self):
        config = _config(self.root)
        archive = load_config(config).paths.archived / 'raw'
        old, _ = self.snapshot(archive, ids=('first', 'second'))
        new, data = self.snapshot(self.root / 'incoming', ids=('first', 'second'))
        data['posts'][0]['nickname'] = 'Changed'
        self.save(new, data)
        with redirect_stdout(self.output), redirect_stderr(io.StringIO()):
            self.assertEqual(archive_exports.main(['--raw-root', str(new.parent), '--config', str(config)]), 0)
            data['posts'].pop(); data['totalPosts'] = 1
            self.save(new, data)
            before = old.read_bytes()
            self.assertEqual(archive_exports.main(['--raw-root', str(new.parent), '--config', str(config), '--move-source', '--force-overwrite']), 3)
        self.assertEqual(old.read_bytes(), before)
        self.assertTrue(new.exists())
        self.assertEqual(len(list(archive.glob('*.json'))), 2)

    def test_archive_failed_publish_preserves_old_and_move_source(self):
        config = _config(self.root)
        archive = load_config(config).paths.archived / 'raw'
        old, _ = self.snapshot(archive)
        new, _ = self.snapshot(self.root / 'incoming', end=date(2026, 1, 1))
        before = old.read_bytes()
        with patch.object(ranges.os, 'replace', side_effect=OSError('injected final failure')), redirect_stdout(self.output), redirect_stderr(io.StringIO()):
            self.assertEqual(archive_exports.main(['--raw-root', str(new.parent), '--config', str(config), '--move-source']), 1)
        self.assertEqual(old.read_bytes(), before)
        self.assertTrue(new.exists())

    def test_unpublished_new_media_blocks_publication_even_if_old_has_no_media(self):
        old, _ = self.snapshot(self.root / 'raw')
        new, data = self.snapshot(self.root / 'incoming', end=date(2026, 1, 1))
        data['posts'][0]['media'][0]['localPath'] = self.media(new.parent)
        self.save(new, data)
        before = old.read_bytes()
        with self.assertRaises(FileNotFoundError):
            self.publish(old, new)
        self.assertEqual(old.read_bytes(), before)
        self.assertFalse((old.parent / new.name).exists())
        self.media(old.parent)
        self.assertFalse(self.publish(old, new).needs_review)
        self.assertFalse(old.exists())

    def test_archive_force_cannot_clobber_cas_bytes_and_valid_control_succeeds(self):
        config = _config(self.root)
        archive = load_config(config).paths.archived / 'raw'
        old, previous = self.snapshot(archive)
        new, incoming = self.snapshot(self.root / 'incoming', end=date(2026, 1, 1))
        reference = self.media(archive)
        previous['posts'][0]['media'][0]['localPath'] = reference
        incoming['posts'][0]['media'][0]['localPath'] = self.media(new.parent)
        self.save(old, previous); self.save(new, incoming)
        (new.parent / reference).write_bytes(b'corrupt incoming object')
        before = old.read_bytes()
        with redirect_stdout(self.output), redirect_stderr(io.StringIO()):
            argv = ['--raw-root', str(new.parent), '--config', str(config), '--force-overwrite']
            self.assertEqual(archive_exports.main(argv), 1)
            self.assertEqual(old.read_bytes(), before)
            self.assertEqual((archive / reference).read_bytes(), b'landed image')
            self.media(new.parent)
            self.assertEqual(archive_exports.main(argv), 0)
        self.assertFalse(old.exists())

    def test_archive_missing_old_media_in_new_snapshot_retains_old_then_full_control_retires(self):
        config = _config(self.root)
        archive = load_config(config).paths.archived / 'raw'
        old, previous = self.snapshot(archive)
        reference = self.media(archive)
        previous['posts'][0]['media'][0]['localPath'] = reference
        self.save(old, previous)
        new, incoming = self.snapshot(self.root / 'incoming', end=date(2026, 1, 1))
        before = old.read_bytes()
        with redirect_stdout(self.output), redirect_stderr(io.StringIO()):
            argv = ['--raw-root', str(new.parent), '--config', str(config)]
            self.assertEqual(archive_exports.main(argv), 3)
            self.assertEqual(old.read_bytes(), before)
            incoming['posts'][0]['media'][0]['localPath'] = self.media(new.parent)
            self.save(new, incoming)
            self.assertEqual(archive_exports.main(argv), 0)
        self.assertFalse(old.exists())

    def test_duplicate_or_empty_old_post_identity_retained_and_valid_control_passes(self):
        old, previous = self.snapshot(self.root / 'raw')
        new, _ = self.snapshot(self.root / 'incoming')
        for malformed in ('duplicate', 'empty'):
            data = copy.deepcopy(previous)
            if malformed == 'duplicate':
                data['posts'].append(copy.deepcopy(data['posts'][0]))
            else:
                data['posts'][0]['id'] = ''
            self.save(old, data)
            self.assertFalse(ranges.compare_coverage(old, new).covered)
        self.save(old, previous)
        self.assertTrue(ranges.compare_coverage(old, new).covered)

    def test_export_cli_returns_review_code_after_successful_publication(self):
        config = _config(self.root)
        old, _ = self.snapshot(self.root / 'raw', ids=('first', 'deleted'))
        result = self.write(old.parent, [_post(self.template, 'first', date(2022, 12, 31))])
        with patch.object(export_bulk_raw, 'WeflowApiBackend') as backend, redirect_stdout(self.output):
            backend.return_value.export_moments_range.return_value = result
            self.assertEqual(export_bulk_raw.main(['--self-moments', '--out', str(old.parent), '--config', str(config)]), ranges.REVIEW_EXIT_CODE)
        self.assertTrue(result.path.exists())


if __name__ == '__main__':
    unittest.main()
