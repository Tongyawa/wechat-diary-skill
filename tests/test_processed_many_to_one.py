"""B' content oracles: expected outputs do not call production target mappings."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts import archive_exports, process_existing_raw
from scripts.run_daily_export import SELF_MOMENTS_SUBROOT
from test_processed_retention import chat, save
from test_moments import _post
from wechat_diary_core.archiving import archive, archive_chats_for
from wechat_diary_core.config import load_config
from wechat_diary_core.preprocessing.moments import archive_moments_for
from wechat_diary_core.workspace import rotate_export_workspace


class ManyToOneRetentionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='retention-many-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        capture = redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def config(self, *, own=False, target=False, subroot='_targets'):
        path = self.root / 'config.toml'
        text = '[paths]\nraw="live"\nprocessed="processed"\narchived="archived"\ninsights="insights"\n[daily_export]\n'
        text += 'self_moments_usernames=' + ('["wxid_synthetic_self"]' if own else '[]') + '\n'
        text += 'target_usernames=' + ('["wxid_target_t"]' if target else '[]') + '\n'
        text += 'target_processed_subroot=' + json.dumps(subroot) + '\n'
        path.write_text(text, encoding='utf-8')
        return load_config(path)

    def moments(self, ids, *, token='MOMENT', username='wxid_synthetic_self', exported='2030-01-02T12:00:00'):
        posts = [_post(pid=n, username=username, create_time=int(datetime(2030, 1, 2, 12).timestamp()),
                       create_time_str='2030/1/2 12:00:00', content=token + '-' + n) for n in ids]
        return dict(exportTime=exported, totalPosts=len(posts), filters=dict(usernames=[username], keyword=''), posts=posts)

    def held_moments(self, *, with_archive_md=True):
        cfg = self.config(own=True)
        name = '朋友圈导出_20300102_abcdef12.json'
        raw = save(cfg.paths.archived / 'raw' / name, self.moments(['one', 'two', 'three']))
        held = save(cfg.paths.raw / name, self.moments(['one', 'two'], exported='2030-01-02T13:00:00'))
        relative = Path(SELF_MOMENTS_SUBROOT) / '2030-01-02.md'
        archived = cfg.paths.archived / 'processed' / relative
        if with_archive_md:
            save(archived, b'ARCHIVED: p1 p2 p3\n')
        live = save(cfg.paths.processed / relative, b'INCOMING: p1 p2\n')
        return cfg, raw, held, relative, archived, live

    def test_2a_self_moments_archive_bytes_stay_and_live_file_is_not_protected(self):
        cfg, raw, held, relative, archived, live = self.held_moments()
        raw_before = raw.read_bytes()
        opened = []
        rotation = rotate_export_workspace(cfg, report_opener=opened.append)
        self.assertEqual(archived.read_bytes(), b'ARCHIVED: p1 p2 p3\n')
        self.assertEqual(live.read_bytes(), b'INCOMING: p1 p2\n')
        self.assertEqual(raw.read_bytes(), raw_before)
        self.assertIn(held, rotation.retained_sources)
        report = (cfg.paths.archived / 'raw-fold-report.md').read_text(encoding='utf-8')
        self.assertIn('多对一日文件：已跳过', report)
        self.assertIn(relative.as_posix(), report)
        self.assertNotIn('process_existing_raw --raw-root <归档会话目录>', report)
        rotate_export_workspace(cfg, report_opener=opened.append)
        self.assertEqual(len(opened), 1, 'identical raw plus processed report must not reopen')
        archive_moments_for(cfg.daily_export.self_moments_usernames, config=cfg,
                            subroot=SELF_MOMENTS_SUBROOT, excluded_sources=rotation.retained_sources)
        self.assertFalse(live.exists())
        self.assertEqual(archived.read_bytes(), b'ARCHIVED: p1 p2 p3\n')
        rotate_export_workspace(cfg)
        self.assertIn(relative.as_posix(), (cfg.paths.archived / 'raw-fold-report.md').read_text(encoding='utf-8'))

    def test_2a_sidecar_keeps_archive_full_version_while_ready_chat_updates(self):
        cfg = self.config(target=True)
        def group(ids):
            data = chat(ids, wxid='room@chatroom', name='Room')
            data['session']['type'] = '群聊'
            for m in data['messages']:
                m['senderUsername'] = 'wxid_target_t'
                m['content'] = 'GROUP-' + str(m['localId'])
            data['messages'][-1].update(isSend=1, senderUsername='wxid_self', content='SELF-ANCHOR')
            return data
        save(cfg.paths.archived / 'raw/Room/Room_20300102.json', group([7, 8, 9]))
        held = save(cfg.paths.raw / 'Room_20300102/Room_20300102.json', group([7, 9]))
        save(cfg.paths.archived / 'raw/Target/Target_20300102.json', chat([1], wxid='wxid_target_t'))
        target = chat([1, 2], wxid='wxid_target_t')
        target['messages'][-1]['content'] = 'TARGET-LATEST'
        save(cfg.paths.raw / 'Target_20300102/Target_20300102.json', target)
        full = save(cfg.paths.archived / 'processed/_targets/chats/2030-01-02.md', b'GROUP-8 AND TARGET-OLD FULL\n')
        save(cfg.paths.archived / 'processed/Room/2030-01-02.md', b'ROOM-FULL')
        save(cfg.paths.archived / 'processed/Target/2030-01-02.md', b'TARGET-OLD')
        archive(cfg.paths.raw, config=cfg)
        archive_chats_for(cfg.daily_export.target_usernames, config=cfg)
        rotate_export_workspace(cfg)
        self.assertTrue(held.exists())
        self.assertEqual(full.read_bytes(), b'GROUP-8 AND TARGET-OLD FULL\n')
        self.assertIn('TARGET-LATEST', (cfg.paths.archived / 'processed/Target/2030-01-02.md').read_text(encoding='utf-8'))
        self.assertIn('TARGET-LATEST', (cfg.paths.processed / '_targets/chats/2030-01-02.md').read_text(encoding='utf-8'))
        report = (cfg.paths.archived / 'raw-fold-report.md').read_text(encoding='utf-8')
        self.assertIn('_targets/chats/2030-01-02.md', report)
        self.assertIn('1:1 会话文件：已跳过', report)
        # A later fresh export still updates both live outputs without F.
        save(cfg.paths.raw / 'Target_20300102/Target_20300102.json', target)
        archive(cfg.paths.raw, config=cfg, excluded_sources={held})
        archive_chats_for(cfg.daily_export.target_usernames, config=cfg, excluded_sources={held})
        self.assertIn('TARGET-LATEST', (cfg.paths.processed/'Target/2030-01-02.md').read_text(encoding='utf-8'))
        process_existing_raw.archive_existing_processed(cfg.paths.processed, cfg.paths.archived/'processed', cfg)
        self.assertIn('TARGET-LATEST', (cfg.paths.processed/'_targets/chats/2030-01-02.md').read_text(encoding='utf-8'))
        self.assertNotIn('GROUP-7', (cfg.paths.processed/'_targets/chats/2030-01-02.md').read_text(encoding='utf-8'))
        self.assertEqual(full.read_bytes(), b'GROUP-8 AND TARGET-OLD FULL\n')

    def test_2c_absent_archive_target_accepts_legacy_render_and_reports_contribution_uncertainty(self):
        cfg, _, held, relative, archived, live = self.held_moments(with_archive_md=False)
        rotate_export_workspace(cfg)
        self.assertEqual(archived.read_bytes(), b'INCOMING: p1 p2\n')
        self.assertFalse(live.exists())
        self.assertTrue(held.exists())
        report = (cfg.paths.archived / 'raw-fold-report.md').read_text(encoding='utf-8')
        self.assertIn('归档无同路径：已并入', report)
        self.assertIn(relative.as_posix(), report)
        self.assertIn('含被保留输入', report)
        self.assertIn(str(held), report)
        self.assertIn('无法据此断定', report)

    def test_2d_non_target_retained_chat_does_not_block_ready_self_moments_same_day(self):
        cfg = self.config(own=True, target=True)
        save(cfg.paths.archived / 'raw/Alpha/Alpha_20300102.json', chat([1, 2, 3], wxid='wxid_victim'))
        held = save(cfg.paths.raw / 'Alpha_20300102/Alpha_20300102.json', chat([1], wxid='wxid_non_target'))
        save(cfg.paths.raw / '朋友圈导出_20300102_new.json', self.moments(['new'], token='READY-SELF'))
        old = save(cfg.paths.archived / 'processed' / SELF_MOMENTS_SUBROOT / '2030-01-02.md', b'OLD-SELF')
        archive_moments_for(cfg.daily_export.self_moments_usernames, config=cfg, subroot=SELF_MOMENTS_SUBROOT)
        rotate_export_workspace(cfg)
        self.assertTrue(held.exists())
        self.assertIn('READY-SELF-new', old.read_text(encoding='utf-8'))
        report = (cfg.paths.archived / 'raw-fold-report.md').read_text(encoding='utf-8')
        self.assertNotIn((Path(SELF_MOMENTS_SUBROOT) / '2030-01-02.md').as_posix(), report)

    def test_nonrotation_and_bulk_processed_entry_reuse_the_same_multi_decision(self):
        cfg, _, _, _, archived, live = self.held_moments()
        rotate_export_workspace(cfg)
        report_path = cfg.paths.archived / 'raw-fold-report.md'
        report_path.write_text('old: process_existing_raw --raw-root <归档会话目录> --day <日>\n' + report_path.read_text(encoding='utf-8'), encoding='utf-8')
        process_existing_raw.archive_existing_processed(cfg.paths.processed, cfg.paths.archived / 'processed', cfg)
        self.assertNotIn('process_existing_raw --raw-root <归档会话目录>', report_path.read_text(encoding='utf-8'))
        self.assertTrue(live.exists())
        self.assertEqual(archived.read_bytes(), b'ARCHIVED: p1 p2 p3\n')
        with redirect_stderr(io.StringIO()):
            rc = archive_exports.main(['--config', str(self.root/'config.toml'), '--processed-root', str(cfg.paths.processed), '--force-overwrite'])
        self.assertEqual(rc, 0)
        self.assertTrue(live.exists())
        self.assertEqual(archived.read_bytes(), b'ARCHIVED: p1 p2 p3\n')

    def test_custom_target_root_does_not_touch_unconfigured_same_day_sidecar(self):
        cfg = self.config(target=True, subroot='custom/nested')
        save(cfg.paths.archived/'raw/Alpha/Alpha_20300102.json', chat([1, 2, 3], wxid='wxid_victim'))
        save(cfg.paths.raw/'Alpha_20300102/Alpha_20300102.json', chat([1], wxid='wxid_target_t'))
        custom = save(cfg.paths.archived/'processed/custom/nested/chats/2030-01-02.md', b'FULL CUSTOM')
        legacy = save(cfg.paths.archived/'processed/_targets/chats/2030-01-02.md', b'OLD LEGACY')
        save(cfg.paths.processed/'custom/nested/chats/2030-01-02.md', b'PARTIAL CUSTOM')
        save(cfg.paths.processed/'_targets/chats/2030-01-02.md', b'LATEST UNTOUCHED LEGACY')
        rotate_export_workspace(cfg)
        self.assertEqual(custom.read_bytes(), b'FULL CUSTOM')
        self.assertEqual(legacy.read_bytes(), b'LATEST UNTOUCHED LEGACY')

    def test_empty_moments_placeholder_before_retention_cannot_empty_archive(self):
        cfg, _, source, relative, archived, live = self.held_moments()
        live.unlink()
        data = json.loads(source.read_bytes())
        data['posts'], data['totalPosts'] = [], 0
        save(source, data)
        process_existing_raw.process_existing_raw(cfg, day='2030-01-02', skip_voice_fallback=True)
        self.assertEqual(live.read_bytes(), b'')
        rotate_export_workspace(cfg)
        self.assertTrue(source.exists())
        self.assertEqual(archived.read_bytes(), b'ARCHIVED: p1 p2 p3\n')
        self.assertTrue(live.exists())
        self.assertIn(relative.as_posix(), (cfg.paths.archived/'raw-fold-report.md').read_text(encoding='utf-8'))

    def test_empty_chat_placeholder_before_retention_cannot_empty_sidecar(self):
        cfg = self.config(target=True)
        save(cfg.paths.archived/'raw/Target/Target_20300102.json', chat([1, 2], wxid='wxid_target_t'))
        source = save(cfg.paths.raw/'Target_20300102/Target_20300102.json', chat([], wxid='wxid_target_t'))
        archived = save(cfg.paths.archived/'processed/_targets/chats/2030-01-02.md', b'FULL TARGET SIDECAR')
        process_existing_raw.process_existing_raw(cfg, day='2030-01-02', skip_voice_fallback=True)
        live = cfg.paths.processed/'_targets/chats/2030-01-02.md'
        self.assertEqual(live.read_bytes(), b'')
        rotate_export_workspace(cfg)
        self.assertTrue(source.exists())
        self.assertEqual(archived.read_bytes(), b'FULL TARGET SIDECAR')
        self.assertTrue(live.exists())

    def test_empty_unrelated_moments_and_numeric_digest_do_not_touch_another_day(self):
        cfg = self.config(own=True, target=True)
        name = '朋友圈导出_20300102_20300103.json'
        old = self.moments(['one'], username='wxid_unrelated')
        save(cfg.paths.archived/'raw'/name, old)
        empty = self.moments([], username='wxid_unrelated', exported='2030-01-02T13:00:00')
        save(cfg.paths.raw/name, empty)
        relative = Path(SELF_MOMENTS_SUBROOT)/'2030-01-03.md'
        archived = save(cfg.paths.archived/'processed'/relative, b'OLD UNRELATED DAY')
        save(cfg.paths.processed/relative, b'READY DIFFERENT DAY')
        rotate_export_workspace(cfg)
        self.assertEqual(archived.read_bytes(), b'READY DIFFERENT DAY')

    def test_empty_related_moments_uses_declared_day_not_numeric_digest(self):
        cfg = self.config(own=True)
        name = '朋友圈导出_20300102_20300103.json'
        save(cfg.paths.archived/'raw'/name, self.moments(['old']))
        save(cfg.paths.raw/name, self.moments([], exported='2030-01-02T13:00:00'))
        ready = self.moments(['ready'], token='READY-DAY-3')
        ready['posts'][0]['createTimeStr'] = '2030/1/3 12:00:00'
        ready['posts'][0]['createTime'] = int(datetime(2030, 1, 3, 12).timestamp())
        save(cfg.paths.raw/'朋友圈导出_20300103_abcdef12.json', ready)
        second = save(cfg.paths.archived/'processed'/SELF_MOMENTS_SUBROOT/'2030-01-02.md', b'FULL DAY 2')
        third = save(cfg.paths.archived/'processed'/SELF_MOMENTS_SUBROOT/'2030-01-03.md', b'OLD DAY 3')
        process_existing_raw.process_existing_raw(cfg, day='2030-01-02', skip_voice_fallback=True)
        rotate_export_workspace(cfg)
        self.assertEqual(second.read_bytes(), b'FULL DAY 2')
        self.assertIn('READY-DAY-3-ready', third.read_text(encoding='utf-8'))
