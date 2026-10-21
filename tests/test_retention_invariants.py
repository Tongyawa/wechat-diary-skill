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

    def render(self, excluded):
        archiving.archive(self.cfg.paths.raw, config=self.cfg, excluded_sources=excluded)
        archiving.archive_chats_for(['wxid_target_t'], config=self.cfg, excluded_sources=excluded)

    def test_victim_day_bytes_are_unchanged_and_ready_chat_merges(self):
        held, victim = self.blocked()
        self.ready()
        self.render(set())
        result = workspace.rotate_export_workspace(self.cfg)
        self.assertIn(isolated_file(self.cfg.paths.archived/'raw', self.cfg.paths.raw, held), result.retained_sources)
        self.assertEqual(victim.read_bytes(), b'VICTIM-ONLY-TOKEN\r\n')
        live = self.cfg.paths.processed / 'Alpha/2030-01-02.md'
        self.assertIn('HELD-GROUP-TOKEN', live.read_text(encoding='utf-8'))
        per.archive_existing_processed(self.cfg.paths.processed, self.cfg.paths.archived / 'processed')
        self.assertIn(b'HELD-GROUP-TOKEN', victim.read_bytes())
        self.assertIn('READY-PRIVATE-TOKEN', (self.cfg.paths.archived / 'processed/Target/2030-01-02.md').read_text(encoding='utf-8'))
        self.render(result.retained_sources)
        self.assertFalse(live.exists(), 'clear_first must remove the skipped legacy render')

    def test_retained_input_stays_three_days_without_partial_sidecar_regeneration(self):
        held, victim = self.blocked()
        self.ready()
        self.render(set())  # Contribution before retention is accepted by the ruling.
        result = workspace.rotate_export_workspace(self.cfg)
        side = self.cfg.paths.archived / 'processed/_targets/chats/2030-01-02.md'
        before = side.read_bytes()
        self.assertIn(b'READY-PRIVATE-TOKEN', before)
        self.assertIn(b'HELD-GROUP-TOKEN', before)
        for day in ('2030-01-03', '2030-01-04', '2030-01-05'):
            with self.subTest(day=day):
                self.ready(day, token='LATEST-' + day)
                self.render(result.retained_sources)
                all_live = '\n'.join(p.read_text(encoding='utf-8') for p in self.cfg.paths.processed.rglob('*.md'))
                self.assertNotIn('HELD-GROUP-TOKEN', all_live)
                self.assertFalse((self.cfg.paths.processed / '_targets/chats/2030-01-02.md').exists())
                result = workspace.rotate_export_workspace(self.cfg)
                self.assertFalse(held.exists())
                self.assertTrue(isolated_file(self.cfg.paths.archived/'raw', self.cfg.paths.raw, held).is_file())
                self.assertEqual(side.read_bytes(), before)
                self.assertEqual(victim.read_bytes(), b'VICTIM-ONLY-TOKEN\r\n')

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
            written = archive_moments_for(['wxid_target_t'], config=self.cfg, subroot=subroot,
                                          excluded_sources=rotation.retained_sources)
            self.assertEqual(written, [])
            self.assertEqual(list((self.cfg.paths.processed / subroot).rglob('*.*')), [])
        per.process_existing_raw(self.cfg, day='2030-01-02', skip_voice_fallback=True)
        self.assertEqual((self.cfg.paths.processed / '_targets/moments/2030-01-02.md').read_bytes(), b'')
        self.assertEqual(victim.read_bytes(), b'ARCHIVED-FULL-MOMENTS')
        self.assertEqual(old.read_bytes(), before)
        self.assertFalse(source.exists() or cas.exists())
        self.assertTrue(isolated_file(self.cfg.paths.archived/'raw', self.cfg.paths.raw, cas).exists())

    def test_manual_uses_ledger_and_excludes_held_input_from_both_renderers(self):
        from scripts.run_daily_export import DailyExportDeps, run_daily_export
        from test_run_daily_export_script import FakeBackend
        self.blocked()
        workspace.rotate_export_workspace(self.cfg)
        self.ready(token='MANUAL-READY')
        with patch('scripts.run_daily_export._open_session_rename_report'):
            run_daily_export(self.cfg, day=date(2030, 1, 2), deps=DailyExportDeps(backend=FakeBackend(name='manual')))
        for relative in ('Target/2030-01-02.md', '_targets/chats/2030-01-02.md'):
            text = (self.cfg.paths.processed / relative).read_text(encoding='utf-8')
            self.assertIn('MANUAL-READY', text)
            self.assertNotIn('HELD-GROUP-TOKEN', text)

    def test_bad_ledger_refuses_all_nonrotation_entries_without_quarantining(self):
        from scripts.run_daily_export import DailyExportDeps, run_daily_export
        from test_run_daily_export_script import FakeBackend
        self.ready()
        live = save(self.cfg.paths.processed / 'Target/2030-01-02.md', b'UNTOUCHED')
        for bad in (b'{"items": [', b'{"schemaVersion":2,"items":[{"details":[null]}]}'):
            ledger = save(self.cfg.paths.archived / 'raw-fold-report.json', bad)
            for action in (lambda: per.process_existing_raw(self.cfg, skip_voice_fallback=True),
                           lambda: per.archive_existing_processed(self.cfg.paths.processed, self.cfg.paths.archived / 'processed'),
                           lambda: run_daily_export(self.cfg, day=date(2030, 1, 2), deps=DailyExportDeps(backend=FakeBackend(name='manual')))):
                with self.assertRaises(workspace.RetentionReportError):
                    action()
                self.assertEqual(live.read_bytes(), b'UNTOUCHED')
                self.assertEqual(ledger.read_bytes(), bad)
                self.assertFalse((self.cfg.paths.archived / 'processed').exists())
            self.assertIn(str(ledger), self.stdout.getvalue())
            self.assertEqual(list(self.cfg.paths.archived.glob('*.corrupt-*.json')), [])

    def test_ledger_missing_or_sources_gone_means_no_exclusions_without_planning(self):
        held, _ = self.blocked()
        with patch.object(workspace, 'plan_raw_ingest', side_effect=AssertionError('no replanning')) as planner:
            self.assertEqual(workspace.retained_raw_sources(self.cfg.paths.archived, live_raw_root=self.cfg.paths.raw), set())
            self.assertEqual(planner.call_count, 0)
        with patch('wechat_diary_core.raw_isolation.isolate_retained'):
            workspace.rotate_export_workspace(self.cfg)
        self.assertEqual(workspace.retained_raw_sources(self.cfg.paths.archived, live_raw_root=self.cfg.paths.raw), {held})
        held.unlink()
        self.assertEqual(workspace.retained_raw_sources(self.cfg.paths.archived, live_raw_root=self.cfg.paths.raw), set())

    def test_preprocessed_iterable_and_mixed_chat_moments_are_excluded(self):
        from test_moments import _post
        source = self.ready()
        data = json.loads(source.read_bytes())
        data.update(exportTime='2030-01-02T10:00:00', totalPosts=1,
                    filters={'usernames':['wxid_target_t'], 'keyword':''},
                    posts=[_post(pid='mixed', username='wxid_target_t', create_time_str='2030/1/2 10:00:00')])
        save(source, data)
        export = ProcessedChatExport(source, source.parent.name, data)
        self.assertEqual(archiving.archive([export], config=self.cfg, excluded_sources={source}), [])
        with patch('wechat_diary_core.preprocessing.preprocess_export', side_effect=AssertionError('must exclude before preprocessing')):
            self.assertEqual(preprocess_run(self.cfg.paths.raw, config=self.cfg, excluded_sources={source}), [])
        self.assertEqual(archive_moments_for(None, config=self.cfg, excluded_sources={source}), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction')
    def test_e4_exclusion_compares_resolved_source_paths_in_both_directions(self):
        source = self.ready()
        link = self.root / 'linked_live'
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(self.cfg.paths.raw)], check=True, capture_output=True)
        self.addCleanup(link.rmdir)
        alias = link / source.relative_to(self.cfg.paths.raw)
        for discovered, excluded in ((link, source), (self.cfg.paths.raw, alias)):
            self.assertEqual(preprocess_run(discovered, config=self.cfg, excluded_sources={excluded}), [])
            self.assertEqual(archiving.archive(discovered, config=self.cfg, excluded_sources={excluded}), [])
            self.assertEqual(archiving.archive_chats_for(['wxid_target_t'], raw_path=discovered, config=self.cfg, excluded_sources={excluded}), [])

    def test_report_has_processed_impact_and_safe_next_step(self):
        self.blocked()
        workspace.rotate_export_workspace(self.cfg)
        report = (self.cfg.paths.archived / 'raw-fold-report.md').read_text(encoding='utf-8')
        self.assertIn('按归档全集', report)
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
                                               excluded_sources={source})
        self.assertFalse(live.exists())
        self.assertEqual(target.read_bytes(), b'PARTIAL BEFORE RETENTION')
