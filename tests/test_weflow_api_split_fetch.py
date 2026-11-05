"""Anonymous fixtures model observed API fields, descending rows and media Map.

Real probe provenance and timing are recorded in the task handoff; no private
identities, filenames or content are carried into these synthetic fixtures.
"""
from collections import Counter
import copy
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from wechat_diary_core.backends.weflow_api import client as api, mapper


DAY = date(2026, 2, 10)
SESSION = {'username': 'split_fixture', 'displayName': 'Fixture'}


def row(identifier, local_id, day, base=3, md5='a', success=True):
    tag = {3: 'img', 47: 'emoji', 34: 'voicemsg', 1: 'text'}[base]
    return {'localId': local_id, 'serverId': identifier, 'localType': base,
            'createTime': int(datetime.combine(day, datetime.min.time()).timestamp()) + 3600,
            'sortSeq': 100, 'isSend': 0, 'senderUsername': 'sender_fixture',
            'content': '[media]' if base != 1 else 'synthetic text',
            'rawContent': f'sender_fixture:\n<msg><{tag} md5="{md5 * 32}" /></msg>',
            'parsedContent': '[media]', '_success': success, '_md5': md5}


class SyntheticApi(api.WeflowApiClient):
    """Model the actual per-page localId Map and persistent voice filename."""
    def __init__(self, rows, root):
        super().__init__('http://example.invalid', 'synthetic-token')
        self.rows = rows
        self.root = root
        self.calls = []
        self.broken = None
        self.exported_payloads = []

    def get_message_page(self, talker, *, start, end, offset=0, limit=10000, media=True):
        self.calls.append((start, end, offset, media))
        def as_day(value):
            return datetime.strptime(api._api_date(value), '%Y%m%d').date() if value else None
        start, end = as_day(start), as_day(end)
        rows = sorted(copy.deepcopy(self.rows), key=lambda m: m['createTime'], reverse=True)
        rows = [m for m in rows if (start is None or datetime.fromtimestamp(m['createTime']).date() >= start)
                and (end is None or datetime.fromtimestamp(m['createTime']).date() <= end)]
        page = rows[offset:offset + limit]
        if media:
            exports = {}
            for message in page:
                base = message['localType']
                if base == 1 or not message['_success']:
                    continue
                kind = {3: 'image', 47: 'emoji', 34: 'voice'}[base]
                filename = ('voice_' + str(message['localId'])) if base == 34 else message['_md5'] * 32
                path = self.root / (filename + ('.wav' if base == 34 else '.gif' if base == 47 else '.png'))
                if not path.exists():
                    path.write_bytes(('bytes:' + message['serverId']).encode())
                exports[message['localId']] = {'mediaType': kind, 'mediaFileName': path.name,
                    'mediaUrl': 'http://example.invalid/media/fixture', 'mediaLocalPath': str(path)}
            for message in page:
                message.update(exports.get(message['localId'], {}))
            self.exported_payloads.extend(copy.deepcopy(page))
        for message in page:
            message.pop('_success'); message.pop('_md5')
        payload = {'success': True, 'count': len(page), 'hasMore': offset + len(page) < len(rows), 'messages': page}
        if media and self.broken == 'missing_has_more':
            payload.pop('hasMore')
        if media and self.broken == 'wrong_count':
            payload['count'] += 1
        if media and self.broken == 'duplicate' and page:
            page.append(dict(page[0])); payload['count'] += 1
        return payload


class SplitFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def fetch(self, rows, *, start=DAY, end=DAY + timedelta(days=1), limit=10000):
        client = SyntheticApi(rows, self.root)
        result = client.fetch_messages(SESSION['username'], start=start, end=end, limit=limit)
        return client, result

    def mapped(self, messages):
        stats = mapper.ImageMediaStats()
        asr = Mock()
        asr.transcribe.return_value = 'synthetic transcript'
        data = mapper.map_session_json(SESSION, messages, contacts=[], media_dir=self.root / '_media',
                                       image_media_stats=stats, transcriber=asr)
        return data['messages'], stats

    def test_cross_day_files_and_unsplit_positive_control(self):
        rows = [row('older', 9, DAY, 47, 'b'), row('newer', 9, DAY + timedelta(days=1))]
        client, checked = self.fetch(rows)
        mapped, stats = self.mapped(checked)
        self.assertEqual([Path(self.root / m['mediaFileName']).read_bytes() for m in checked],
                         [b'bytes:newer', b'bytes:older'])
        self.assertTrue(all(m['source'] for m in mapped))
        self.assertEqual(stats.media_split_segments, 2)
        self.assertEqual(Counter((m['serverId'], m['localId']) for m in checked),
                         Counter((m['serverId'], m['localId']) for m in rows))
        self.assertEqual([m['serverId'] for m in checked], ['newer', 'older'])
        self.assertEqual([m['platformMessageId'] for m in mapped], ['older', 'newer'])
        for start, end, _, media in client.calls:
            if media:
                self.assertEqual(start, end)
        unsplit = client._fetch_message_range(SESSION['username'], start=DAY, end=DAY + timedelta(days=1), limit=10000, media=True)
        self.assertTrue(any(not m['source'] for m in self.mapped(unsplit)[0]))

    def test_all_rows_and_pages_participate_even_nonmedia_and_failed_exports(self):
        for base, success in ((1, True), (3, False)):
            with self.subTest(base=base):
                rows = [row('older', 5, DAY, base, 'b', success), row('newer', 5, DAY + timedelta(days=1))]
                client, checked = self.fetch(rows, limit=1)
                media_calls = [c for c in client.calls if c[3]]
                self.assertTrue(all(c[0] == c[1] for c in media_calls))
                self.assertEqual(len(checked), 2)
                older = next(m for m in checked if m['serverId'] == 'older')
                self.assertFalse(older.get('mediaLocalPath'))
                self.assertTrue(next(m for m in checked if m['serverId'] == 'newer').get('mediaLocalPath'))

    def test_same_day_guard_remains_and_counts_rejected_rows(self):
        rows = [row('one', 8, DAY), row('two', 8, DAY, md5='b')]
        client, checked = self.fetch(rows, end=DAY)
        expected = mapper.guard_message_page(client.exported_payloads)
        self.assertEqual([m.media_rejection for m in checked], [m.media_rejection for m in expected])
        mapped, stats = self.mapped(checked)
        self.assertEqual(sum(not m['source'] for m in mapped), 1)
        self.assertEqual(stats.rejected_same_day_collisions, 1)
        self.assertEqual(stats.media_split_segments, 0)
        self.assertEqual(len(client.calls), 1)

    def test_single_day_no_voice_has_zero_extra_requests(self):
        client, checked = self.fetch([row('only', 8, DAY)], end=DAY)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(self.mapped(checked)[0][0]['source'])

    def test_no_collision_multiday_does_not_split_and_preserves_stable_ties(self):
        rows = [row('shared-server', 2, DAY), row('shared-server', 3, DAY)]
        client, checked = self.fetch(rows)
        self.assertEqual(len([c for c in client.calls if c[3]]), 1)
        self.assertEqual([m['localId'] for m in self.mapped(checked)[0]], [2, 3])
        self.assertEqual(sum(getattr(m, 'fetch_split_segments', 0) for m in checked), 0)

    def test_voice_cached_other_day_is_rejected_but_unique_cache_is_allowed(self):
        old = self.root / 'voice_7.wav'; old.write_bytes(b'old-recording')
        os.utime(old, ns=(1_000_000_000, 1_000_000_000))
        rows = [row('old', 7, DAY, 34), row('new', 7, DAY + timedelta(days=1), 34)]
        client, checked = self.fetch(rows, start=DAY + timedelta(days=1))
        mapped, stats = self.mapped(checked)
        self.assertEqual(mapped[0]['source'], '')
        self.assertIn('媒体缺失', mapped[0]['content'])
        self.assertEqual(stats.rejected_voice_cache, 1)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(old.read_bytes(), b'old-recording')
        _, unique = self.fetch([rows[1]], start=DAY + timedelta(days=1))
        self.assertTrue(self.mapped(unique)[0][0]['source'])

    def test_cross_day_voice_second_cache_hit_rejected_fresh_first_allowed(self):
        rows = [row('old', 7, DAY, 34), row('new', 7, DAY + timedelta(days=1), 34)]
        _, checked = self.fetch(rows)
        mapped, stats = self.mapped(checked)
        by_id = {m['platformMessageId']: m for m in mapped}
        self.assertTrue(by_id['old']['source'])
        self.assertEqual(by_id['new']['source'], '')
        self.assertEqual(stats.rejected_voice_cache, 1)

    def test_index_failure_degrades_voice_without_losing_messages(self):
        old = self.root / 'voice_7.wav'; old.write_bytes(b'old-recording')
        os.utime(old, ns=(1_000_000_000, 1_000_000_000))
        client = SyntheticApi([row('only', 7, DAY, 34)], self.root)
        original = client.get_message_page
        def fail_index(talker, **kwargs):
            if kwargs['start'] is None:
                raise api.WeflowApiError('synthetic failure')
            return original(talker, **kwargs)
        with patch.object(client, 'get_message_page', side_effect=fail_index):
            checked = client.fetch_messages(SESSION['username'], start=DAY, end=DAY)
        self.assertEqual(len(checked), 1)
        self.assertEqual(self.mapped(checked)[1].rejected_voice_cache, 1)

    def test_coarse_clock_equal_to_request_start_cannot_prove_new_voice(self):
        path = self.root / 'voice_7.wav'
        path.write_bytes(b'synthetic-old-voice')
        stamp = 1_700_000_000_000_000_000
        os.utime(path, ns=(stamp, stamp))
        rows = [row('old', 7, DAY, 34), row('new', 7, DAY + timedelta(days=1), 34)]
        with patch.object(api.time, 'time_ns', return_value=stamp):
            _, checked = self.fetch(rows, start=DAY + timedelta(days=1))
        self.assertEqual(self.mapped(checked)[1].rejected_voice_cache, 1)

    def test_previously_seen_voice_path_is_not_new_after_clock_moves_back(self):
        rows = [row('old', 7, DAY, 34), row('new', 7, DAY + timedelta(days=1), 34)]
        client = SyntheticApi(rows, self.root)
        original = client.get_message_page
        start = 1_700_000_000_000_000_000
        def future_stamp(talker, **kwargs):
            payload = original(talker, **kwargs)
            if kwargs['media']:
                os.utime(self.root / 'voice_7.wav', ns=(start + 10_000, start + 10_000))
            return payload
        # Both pages see the same time window around the same unchanged file.
        clock = iter([start, start + 20_000] * 10)
        with patch.object(client, 'get_message_page', side_effect=future_stamp), \
                patch.object(api.time, 'time_ns', side_effect=lambda: next(clock)):
            checked = client.fetch_messages(SESSION['username'], start=DAY, end=DAY + timedelta(days=1))
        rows, stats = self.mapped(checked)
        self.assertTrue(rows[0]['source'])
        self.assertEqual(rows[1]['source'], '')
        self.assertEqual(stats.rejected_voice_cache, 1)

    def test_split_pagination_completeness_failures_still_raise(self):
        for broken in ('missing_has_more', 'wrong_count', 'duplicate'):
            with self.subTest(broken=broken):
                client = SyntheticApi([row('old', 2, DAY), row('new', 2, DAY + timedelta(days=1))], self.root)
                client.broken = broken
                with self.assertRaises(api.WeflowApiError):
                    client.fetch_messages(SESSION['username'], start=DAY, end=DAY + timedelta(days=1), limit=1)

    def test_retrieval_identity_drift_is_not_silently_published(self):
        client = SyntheticApi([row('old', 2, DAY), row('new', 2, DAY + timedelta(days=1))], self.root)
        original = client.get_message_page
        def missing(talker, **kwargs):
            payload = original(talker, **kwargs)
            if kwargs['media']:
                payload['messages'] = []; payload['count'] = 0
            return payload
        with patch.object(client, 'get_message_page', side_effect=missing):
            with self.assertRaisesRegex(api.WeflowApiError, '身份不一致'):
                client.fetch_messages(SESSION['username'], start=DAY, end=DAY + timedelta(days=1))

    def test_single_day_duplicate_ids_are_refused_before_mapping(self):
        client = SyntheticApi([row('only', 2, DAY)], self.root)
        client.broken = 'duplicate'
        with self.assertRaisesRegex(api.WeflowApiError, '重复消息 ID'):
            client.fetch_messages(SESSION['username'], start=DAY, end=DAY)

    def test_local_midnight_boundaries_do_not_drop_or_duplicate_messages(self):
        rows = [row('before', 2, DAY), row('midnight', 2, DAY + timedelta(days=1))]
        midnight = int(datetime.combine(DAY + timedelta(days=1), datetime.min.time()).timestamp())
        rows[0]['createTime'], rows[1]['createTime'] = midnight - 1, midnight
        _, checked = self.fetch(rows)
        self.assertEqual(identities := Counter((m['serverId'], m['localId']) for m in checked),
                         Counter((m['serverId'], m['localId']) for m in rows))
        self.assertEqual(sum(identities.values()), 2)


if __name__ == '__main__':
    unittest.main()
