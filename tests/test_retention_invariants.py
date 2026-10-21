"""Content-oracle tests: expected paths/tokens never use the production mapping."""
from raw_isolation_support import isolated_file
from contextlib import redirect_stdout
from datetime import date
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_processed_retention import chat, save
from scripts import process_existing_raw as per
from wechat_diary_core import archiving, workspace
from wechat_diary_core.config import load_config
from wechat_diary_core.preprocessing import ProcessedChatExport, run as preprocess_run
from wechat_diary_core.preprocessing.moments import archive_moments_for


class RetentionInvariants(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='retention-r6-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config_path = self.root / 'config.toml'
        self.config_path.write_text('[paths]\nraw="live"\nprocessed="processed"\narchived="archived"\ninsights="insights"\n'
            '[daily_export]\ntarget_usernames=["wxid_target_t"]\n', encoding='utf-8')
        self.cfg = load_config(self.config_path)
        self.stdout = io.StringIO()
        context = redirect_stdout(self.stdout)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def blocked(self):
        save(self.cfg.paths.archived / 'raw/Alpha/Alpha_20300102.json', chat([1, 2, 3], wxid='wxid_victim'))
        group = chat([7, 8, 9], wxid='grp_other@chatroom')
        group['session']['type'] = '群聊'
        for message in group['messages']:
            message['content'] = 'HELD-GROUP-TOKEN'
            message['senderUsername'] = 'wxid_target_t'
        group['messages'][-1].update(isSend=1, senderUsername='wxid_self', content='SELF-GROUP-ANCHOR')
        held = save(self.cfg.paths.raw / 'Alpha_20300102/Alpha_20300102.json', group)
        victim = save(self.cfg.paths.archived / 'processed/Alpha/2030-01-02.md', b'VICTIM-ONLY-TOKEN\r\n')
        return held, victim

    def ready(self, day='2030-01-02', token='READY-PRIVATE-TOKEN'):
        data = chat([1, 2], wxid='wxid_target_t', day=day)
        data['messages'][-1]['content'] = token
        return save(self.cfg.paths.raw / f'Target_{day.replace("-", "")}/Target_{day.replace("-", "")}.json', data)

    def render(self):
        archiving.archive(self.cfg.paths.raw, config=self.cfg)
        archiving.archive_chats_for(['wxid_target_t'], config=self.cfg)

    def test_victim_day_bytes_are_unchanged_and_ready_chat_merges(self):
        held, victim = self.blocked()
        self.ready()
        self.render()
        result = workspace.rotate_export_workspace(self.cfg)
        self.assertIn(isolated_file(self.cfg.paths.archived/'raw', self.cfg.paths.raw, held), result.retained_sources)
        self.assertEqual(victim.read_bytes(), b'VICTIM-ONLY-TOKEN\r\n')
        live = self.cfg.paths.processed / 'Alpha/2030-01-02.md'
        self.assertIn('HELD-GROUP-TOKEN', live.read_text(encoding='utf-8'))
        per.archive_existing_processed(self.cfg.paths.processed, self.cfg.paths.archived / 'processed')
        self.assertIn(b'HELD-GROUP-TOKEN', victim.read_bytes())
        self.assertIn('READY-PRIVATE-TOKEN', (self.cfg.paths.archived / 'processed/Target/2030-01-02.md').read_text(encoding='utf-8'))
        self.render()
        self.assertFalse(live.exists(), 'clear_first must remove the skipped legacy render')


    def test_ready_latest_content_reaches_every_output_while_held_content_is_excluded(self):
        held, _ = self.blocked()
        result = workspace.rotate_export_workspace(self.cfg)
        for token in ('READY-NEW-TEXT', 'READY-VOICE-TRANSCRIBED'):
            with self.subTest(token=token):
                self.ready(token=token)
                per.process_existing_raw(self.cfg, day='2030-01-02', skip_voice_fallback=True)
                for relative in ('Target/2030-01-02.md', '_targets/chats/2030-01-02.md'):
                    content = (self.cfg.paths.processed / relative).read_text(encoding='utf-8')
                    self.assertIn(token, content)
                    self.assertNotIn('HELD-GROUP-TOKEN', content)
                self.assertFalse((self.cfg.paths.processed / 'Alpha/2030-01-02.md').exists())
        self.assertFalse(held.exists())
        self.assertTrue(isolated_file(self.cfg.paths.archived/'raw', self.cfg.paths.raw, held).exists())

    def test_e1_retained_moments_produce_no_output_and_keep_cas(self):
        from test_moments import _post
        import hashlib
        digest = hashlib.md5(b'SYNTHETIC-IMAGE').hexdigest()
        media = f'_media/images/{digest[:2]}/{digest}.jpg'
        def payload(ids):
            posts = [_post(pid=p, username='wxid_target_t', content='HELD-MOMENT-' + p,
                           create_time_str='2030/1/2 10:00:00') for p in ids]
            for post in posts:
                post['media'] = [{'localPath': media}]
            return dict(exportTime='2030-01-02T10:00:00', totalPosts=len(posts),
                        filters=dict(usernames=['wxid_target_t'], keyword=''), posts=posts)
        name = '朋友圈导出_20300102_abcdef12.json'
        old = save(self.cfg.paths.archived / 'raw' / name, payload(['one', 'two']))
        source = save(self.cfg.paths.raw / name, payload(['one']))
        cas = save(self.cfg.paths.raw / media, b'SYNTHETIC-IMAGE')
        victim = save(self.cfg.paths.archived / 'processed/_targets/moments/2030-01-02.md', b'ARCHIVED-FULL-MOMENTS')
        before = old.read_bytes()
        rotation = workspace.rotate_export_workspace(self.cfg)
        self.assertIn(isolated_file(self.cfg.paths.archived/'raw', self.cfg.paths.raw, source), rotation.retained_sources)
        for subroot in ('_targets/moments', '朋友圈_自己'):
            written = archive_moments_for(['wxid_target_t'], config=self.cfg, subroot=subroot)
            self.assertEqual(written, [])
            self.assertEqual(list((self.cfg.paths.processed / subroot).rglob('*.*')), [])
        per.process_existing_raw(self.cfg, day='2030-01-02', skip_voice_fallback=True)
        self.assertEqual((self.cfg.paths.processed / '_targets/moments/2030-01-02.md').read_bytes(), b'')
        self.assertEqual(victim.read_bytes(), b'ARCHIVED-FULL-MOMENTS')
        self.assertEqual(old.read_bytes(), before)
        self.assertFalse(source.exists() or cas.exists())
        self.assertTrue(isolated_file(self.cfg.paths.archived/'raw', self.cfg.paths.raw, cas).exists())






    def test_report_has_processed_impact_and_safe_next_step(self):
        self.blocked()
        workspace.rotate_export_workspace(self.cfg)
        report = (self.cfg.paths.archived / 'raw-fold-report.md').read_text(encoding='utf-8')
        self.assertIn('手工按日重生', report)
        self.assertIn('不要用 `process_existing_raw` 单目录', report)
        self.assertNotIn('process_existing_raw --raw-root <归档会话目录>', report)
        self.assertIn('下次轮转自动清项', report)

    def test_unconfigured_legacy_moments_path_is_not_touched(self):
        # A legacy output outside the configured renderers is not touched.
        from test_moments import _post
        source = save(self.cfg.paths.raw / 'moments.json', dict(exportTime='now', totalPosts=1,
            filters={'usernames':['wxid_target_t'], 'keyword':''},
            posts=[_post(pid='held', username='wxid_target_t', create_time_str='2030/1/2 10:00:00')]))
        target = save(self.cfg.paths.archived / 'processed/_self/moments/2030-01-02.md', b'FULL BEFORE')
        live = save(self.cfg.paths.processed / '_self/moments/2030-01-02.md', b'PARTIAL BEFORE RETENTION')
        workspace.merge_processed_into_archive(self.cfg.paths.processed, self.cfg.paths.archived / 'processed',
                                               retained_sources={source}, config=self.cfg)
        self.assertFalse(live.exists())
        self.assertEqual(target.read_bytes(), b'PARTIAL BEFORE RETENTION')
