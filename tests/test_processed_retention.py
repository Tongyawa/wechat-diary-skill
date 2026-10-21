"""Synthetic reproductions of the four accepted processed/report findings."""
from contextlib import redirect_stdout
from datetime import date
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import process_existing_raw, archive_exports
from wechat_diary_core.config import load_config
from wechat_diary_core.workspace import rotate_export_workspace
from wechat_diary_core import raw_archive as ra
from wechat_diary_core.raw_schema import validate_moments_json


def chat(ids, *, wxid='wxid_synthetic_a', day='2030-01-02', name='Synthetic'):
    session = dict(wxid=wxid, username=wxid, displayName=name, nickname=name,
                   remark='', type='私聊', messageCount=len(ids))
    messages = [dict(localId=n, createTime=1000+n, formattedTime=f'{day} 10:00:00',
                     type='文本消息', content=f'text-{n}', source='', isSend=0,
                     senderUsername='wxid_synthetic_sender', senderDisplayName='S',
                     senderRemark='', senderNickname='S', platformMessageId=f'pm-{n}',
                     senderGroupNickname='') for n in ids]
    return dict(session=session, messages=messages, weflow=dict(source='http_api', mediaScheme='cas-v1'))


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, indent=2).encode())
    return path


class ProcessedRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='processed-retention-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'config.toml'
        self.config_path.write_text('[paths]\nraw="live"\nprocessed="processed"\narchived="archived"\ninsights="insights"\n', encoding='utf-8')
        self.cfg = load_config(self.config_path)
        self.archive = self.root / 'archived/raw'


    def blocked_chat(self):
        victim = save(self.archive / 'Alpha/Alpha_20300102.json', chat([1,2,3,4,5], wxid='wxid_victim_w1'))
        incoming = save(self.cfg.paths.raw / 'Alpha_20300102/Alpha_20300102.json', chat([1,2], wxid='wxid_other_w2'))
        old_md = save(self.root / 'archived/processed/Alpha/2030-01-02.md', b'VICTIM W1 RENDER\n')
        live_md = save(self.cfg.paths.processed / 'Alpha/2030-01-02.md', b'OTHER W2 RENDER\n')
        return victim, incoming, old_md, live_md


    def test_e2_existing_processed_cannot_merge_withheld_chat(self):
        victim, incoming, old_md, live_md = self.blocked_chat()
        before = victim.read_bytes()
        with redirect_stdout(io.StringIO()):
            rotate_export_workspace(self.cfg, report_opener=None)
            self.assertTrue(incoming.exists() and live_md.exists())
            process_existing_raw.archive_existing_processed(self.cfg.paths.processed, self.root / 'archived/processed')
        self.assertEqual(victim.read_bytes(), before)
        self.assertEqual(old_md.read_bytes(), b'VICTIM W1 RENDER\n')
        self.assertTrue(live_md.exists())


    def test_moments_cas_retention_ignores_metadata_and_recovers_complete_prefix(self):
        for truncated in (False, 'post', 'media', 'live_photo'):
            with self.subTest(truncated=truncated):
                root = self.root / str(truncated)
                live, archive = root / 'live', root / 'archived/raw'
                used_hash, extra_hash = hashlib.md5(b'used').hexdigest(), hashlib.md5(b'extra').hexdigest()
                used = f'_media/images/{used_hash[:2]}/{used_hash}.jpg'
                extra = f'_media/images/{extra_hash[:2]}/{extra_hash}.jpg'
                movie_hash = hashlib.md5(b'live photo movie').hexdigest()
                movie = f'_media/videos/{movie_hash[:2]}/{movie_hash}.mp4'
                name = '朋友圈导出_20300102_abcdef12.json'
                old = save(archive / name, {'exportTime':1,'posts':[{'id':'old'}]})
                before = old.read_bytes()
                payload = {'exportTime':2,'metadata':extra,'posts':[{'id':'new','contentDesc':extra,
                           'media':[{'localPath':used,'thumbnail':extra,'livePhoto':{'localPath':movie}}]}]}
                data = json.dumps(payload).encode()
                if truncated == 'post':
                    data = data[:-2] + b', {"id":"incomplete'
                elif truncated == 'media':
                    data = data[:data.index(b'"thumbnail":')] + b'"thumbnail":'
                elif truncated == 'live_photo':
                    end = data.index(json.dumps(movie).encode()) + len(json.dumps(movie).encode())
                    data = data[:end] + b', "incomplete":'
                incoming = save(live / name, data)
                kept, moved = save(live / used, b'used'), save(live / extra, b'extra')
                kept_movie = save(live / movie, b'live photo movie')
                plan = ra.plan_raw_ingest(archive, [live], policy='daily')
                action = next(a for a in plan.actions if a.source == incoming)
                self.assertNotEqual(action.status, 'ready')
                self.assertEqual(action.cas_references, {kept} if truncated == 'media' else {kept, kept_movie})
                ra.apply_raw_ingest_plan(plan, move_source=True)
                self.assertEqual(old.read_bytes(), before)
                self.assertTrue(incoming.exists() and kept.exists())
                self.assertEqual(kept_movie.exists(), truncated != 'media')
                self.assertFalse(moved.exists())


    def test_report_render_and_write_exceptions_do_not_abort_safe_merge(self):
        self.blocked_chat()
        original = ra._atomic_bytes
        for failure in ('render', 'json', 'markdown'):
            with self.subTest(failure=failure):
                # Force the exact report write under test; an unchanged report
                # deliberately performs no write and emits no warning.
                save(self.root / 'archived/raw-fold-report.json', {'schemaVersion':2,'items':[]})
                save(self.root / 'archived/raw-fold-report.md', b'STALE REPORT')
                good = save(self.cfg.paths.raw / 'Beta_20300102/Beta_20300102.json', chat([1], wxid='wxid_beta'))
                save(self.cfg.paths.processed / 'Beta/2030-01-02.md', failure.encode())
                def writer(path, data):
                    if path.name == ('raw-fold-report.json' if failure == 'json' else 'raw-fold-report.md'):
                        raise RuntimeError('synthetic report persistence failure')
                    return original(path, data)
                output = io.StringIO()
                target = patch.object(ra, '_report_markdown', side_effect=KeyError('synthetic render failure')) if failure == 'render' else patch.object(ra, '_atomic_bytes', side_effect=writer)
                with target as patched, redirect_stdout(output):
                    rotate_export_workspace(self.cfg, report_opener=None)
                self.assertTrue(patched.called, 'the injected report failure must actually run')
                self.assertIn('报告未能更新', output.getvalue())
                self.assertFalse(good.exists())
                self.assertEqual((self.root / 'archived/processed/Beta/2030-01-02.md').read_bytes(), failure.encode())


    def test_report_schema_rejects_wrong_values_and_nested_shapes(self):
        self.blocked_chat()
        for item in ({'reasons':'not a list'}, {'identity':['not a mapping']},
                     {'details':[None]}, {'details':[{'incoming':{}, 'legacyTarget':{'identity':[]}}]},
                     {'archiveFiles':[3]}):
            with self.subTest(item=item):
                path = save(self.root / 'archived/raw-fold-report.json', {'schemaVersion':2,'items':[item]})
                before = path.read_bytes()
                with redirect_stdout(io.StringIO()):
                    rotate_export_workspace(self.cfg, report_opener=None)
                self.assertTrue(any(p.read_bytes() == before for p in path.parent.glob('raw-fold-report.corrupt-*.json')))
                ra.validate_archive_report(json.loads(path.read_bytes()))


    def test_e3_malformed_old_detail_is_quarantined_without_aborting_rotation(self):
        self.blocked_chat()
        report = {'schemaVersion':2, 'items':[{'entrypoint':'daily',
                  'identity':{'wxid':'wxid_old_item','day':'2030-01-01'},
                  'sources':[], 'targets':[], 'archiveFiles':[], 'reasons':['final_target_other_identity'],
                  'details':[{'incoming':{'path':'x','identity':{'wxid':'wxid_old_item','day':'2030-01-01'}}}]}]}
        report_path = save(self.root / 'archived/raw-fold-report.json', report)
        before = report_path.read_bytes()
        good = save(self.cfg.paths.raw / 'Beta_20300102/Beta_20300102.json', chat([1], wxid='wxid_beta'))
        good_md = save(self.cfg.paths.processed / 'Beta/2030-01-02.md', b'BETA RENDER\n')
        output = io.StringIO()
        with redirect_stdout(output):
            rotate_export_workspace(self.cfg, report_opener=None)
            rotate_export_workspace(self.cfg, report_opener=None)
        corrupt, = (self.root / 'archived').glob('raw-fold-report.corrupt-*.json')
        self.assertEqual(corrupt.read_bytes(), before)
        self.assertIn('[WARN]', output.getvalue())
        self.assertFalse(good.exists() or good_md.exists())
        self.assertEqual((self.root / 'archived/processed/Beta/2030-01-02.md').read_bytes(), b'BETA RENDER\n')
        self.assertTrue((self.root / 'archived/raw-fold-report.md').is_file())


