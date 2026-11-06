from __future__ import annotations

import copy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from wechat_diary_core.archiving import archive
from wechat_diary_core.backends.weflow_api.mapper import map_session_json
from wechat_diary_core.chat_flow import render_message_content
from wechat_diary_core.config import load_config
from wechat_diary_core.emoji import emoji_md5
from wechat_diary_core.preprocessing.cleaner import prepare_chat_messages
from wechat_diary_core.preprocessing.vision_units import ImageItem, chat_body

ROOT = Path(__file__).resolve().parents[1]
REFS = json.loads((ROOT / 'tests/fixtures/emoji_label_refs.json').read_text(encoding='utf-8'))
MD5 = 'a' * 32


def raw_data(ref='cas', name='原名'):
    data = map_session_json({'username': 'contact_placeholder', 'displayName': '示例'},
        [{'localId': 1, 'createTime': 1793757600, 'localType': 47, 'isSend': 0}], contacts=[])
    data['messages'][0].update(REFS[ref])
    data['messages'][0]['formattedTime'] = '2026-11-04 10:00:00'
    if name is not None:
        data['messages'][0]['emojiName'] = name
    return data


class EmojiLabelTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tests/_artifacts').mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tests/_artifacts')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'labels.json'
        config = self.root / 'config.toml'
        config.write_text('[emoji_labels]\npath="labels.json"\n', encoding='utf-8')
        self.config = load_config(config)

    def table(self, entry=None, *, data=None):
        self.path.write_text(json.dumps(data if data is not None else
            {'version': 1, 'labels': {MD5: entry or {}}}, ensure_ascii=False), encoding='utf-8')

    def final(self, data=None, config=None):
        source = self.root / '私聊_示例_20261104.json'
        source.write_text(json.dumps(data or raw_data(), ensure_ascii=False), encoding='utf-8')
        before = source.read_bytes()
        files = archive(source, config=config or self.config, output_root=self.root / 'processed',
                        clear_first=False, image_mode='preserve_paths')
        self.assertEqual(len(files), 1)
        self.assertEqual(source.read_bytes(), before)
        self.output = files[0]
        return files[0].read_text(encoding='utf-8')

    def test_four_level_priority_in_final_processed(self):
        cases = [({'text': '转写', 'name': '表名', 'album': '专辑'}, '原名', '〔转写·专辑〕'),
                 ({'text': '转写'}, '原名', '〔转写〕'),
                 ({'name': '表名', 'album': '专辑'}, '原名', '〔原名·专辑〕'),
                 ({'name': '表名'}, None, '〔表名〕'),
                 ({'name': '表名', 'album': '专辑'}, '', '〔表名·专辑〕'),
                 ({}, '原名', '〔原名〕'),
                 ({'album': '专辑'}, None, '〔专辑〕'), ({}, None, '[表情]')]
        for entry, name, expected in cases:
            with self.subTest(entry=entry, name=name):
                self.table(entry)
                self.assertIn('：' + expected + '\n', self.final(raw_data(name=name), load_config(self.root / 'config.toml')))

    def test_observed_cas_and_legacy_reach_final_processed(self):
        self.table({'text': '中性转写'})
        for shape in ('cas', 'legacy'):
            for field in (('source', 'content') if shape == 'cas' else ('emojiCdnUrl',)):
                data = raw_data(shape)
                if shape == 'cas':
                    data['messages'][0]['content' if field == 'source' else 'source'] = ''
                with self.subTest(shape=shape, field=field):
                    self.assertEqual(emoji_md5(data['messages'][0]), MD5)
                    self.assertIn('：〔中性转写〕\n', self.final(raw_data(shape)))
        for message in ({'source': '../_media/images/aa/' + MD5 + '.gif'},
                        {'source': '../_media/emojis/bb/' + MD5 + '.gif'},
                        {'emojiCdnUrl': 'https://emoji.invalid/?m=' + MD5.upper()},
                        {'emojiCdnUrl': 'https://[bad/?m=' + MD5},
                        {'emojiCdnUrl': 'https://emoji.invalid/?m=' + MD5 + '&m=' + MD5}):
            self.assertIsNone(emoji_md5(message))

    def test_three_consumers_agree_and_raw_unchanged(self):
        self.table({'text': '中性转写', 'album': '中性专辑'})
        data = raw_data()
        data['messages'].append(dict(data['messages'][0], type='文本消息', content='示例正文',
                                     source='', localId=2, createTime=1793757601))
        original = copy.deepcopy(data)
        for skip in (True, False):
            cfg = replace(self.config, preprocessing=replace(self.config.preprocessing, skip_emoji_dir=skip))
            messages = prepare_chat_messages(data, cfg)
            if skip:
                self.assertEqual(messages[0]['content'], '〔中性转写·中性专辑〕')
            self.assertEqual(render_message_content(messages[0]), '〔中性转写·中性专辑〕')
            image = dict(messages[0], type='图片消息', content='[图片]', source='')
            item = ImageItem(Path('unused'), image, 1, 1, image['createTime'], image['senderUsername'],
                             image['formattedTime'][:10], 'sha', 'md5', (1, 1), None)
            self.assertIn('〔中性转写·中性专辑〕', chat_body([messages[0], image], [item], cfg.preprocessing.image_vision))
            self.assertIn('：〔中性转写·中性专辑〕 | 示例正文\n', self.final(data, cfg))
        self.assertEqual(data, original)
        self.assertNotIn('_emoji_label', data['messages'][0])

    def test_missing_broken_unknown_tables_warn_once_and_export(self):
        cases = [None, '{', '[]', '{"version":2,"labels":{}}',
                 '{"version":true,"labels":{}}', '{"version":1.0,"labels":{}}',
                 '{"version":1,"labels":[]}', '\udcff']
        for content in cases:
            with self.subTest(content=repr(content)):
                self.path.unlink(missing_ok=True)
                if content is not None:
                    self.path.write_bytes(content.encode('utf-8', errors='surrogatepass'))
                cfg = load_config(self.root / 'config.toml')
                with self.assertLogs('wechat_diary_core.emoji', level='WARNING') as logs:
                    self.assertIn('：〔原名〕\n', self.final(config=cfg))
                    self.assertIn('：〔原名〕\n', self.final(config=cfg))
                self.assertEqual(len(logs.output), 1)
                self.assertIn('[WARN] 表情标签表', logs.output[0])

    def test_bad_entries_are_isolated_and_warn_once(self):
        for bad in (None, [], 12, {'text': 5}, {'album': []}, {'name': {}}, {'text': False}):
            with self.subTest(bad=bad):
                self.table(data={'version': 1, 'labels': {MD5: bad, 'b' * 32: {'text': '正常项'}}})
                cfg = load_config(self.root / 'config.toml')
                with self.assertLogs('wechat_diary_core.emoji', level='WARNING') as logs:
                    self.assertIn('：〔原名〕\n', self.final(config=cfg))
                    other = raw_data()
                    other['messages'][0].update(source='../_media/emojis/bb/' + 'b' * 32 + '.gif',
                                                content='../_media/emojis/bb/' + 'b' * 32 + '.gif')
                    self.assertIn('：〔正常项〕\n', self.final(other, cfg))
                self.assertEqual(len(logs.output), 1)

    def test_invalid_md5_keys_are_ignored(self):
        self.table(data={'version': 1, 'labels': {key: {'text': '不可命中'} for key in
            (MD5.upper(), MD5[:-1], MD5 + 'a', 'g' * 32, ' ' + MD5, MD5 + '\n')}})
        with self.assertLogs('wechat_diary_core.emoji', level='WARNING') as logs:
            self.assertIn('：〔原名〕\n', self.final())
        self.assertEqual(self.config.emoji_labels.store.labels, {})
        self.assertEqual(len(logs.output), 1)

    def test_safe_single_segment_and_album_shortened_first(self):
        self.table({'text': '中\n性 | 〔转写〕·内容', 'album': '辑\r\n | 〔名〕·附'})
        with self.assertLogs('wechat_diary_core.emoji', level='WARNING'):
            self.assertIn('：〔中 性 ｜ （转写）·内容·辑 ｜ （名）·附〕\n', self.final())
        for text, album, expected in [('主' * 75, '辑' * 20, '主' * 75 + '·辑辑辑…'),
                                      ('主' * 79, '辑', '主' * 79),
                                      ('主' * 100, '辑', '主' * 79 + '…'),
                                      ('', '辑' * 100, '辑' * 79 + '…')]:
            with self.subTest(text=len(text), album=len(album)):
                self.table({'text': text, 'album': album})
                cfg = load_config(self.root / 'config.toml')
                with self.assertLogs('wechat_diary_core.emoji', level='WARNING') if max(len(text),len(album)) > 80 else self.assertNoLogs('wechat_diary_core.emoji'):
                    self.assertIn('：〔' + expected + '〕\n', self.final(raw_data(name=None), cfg))

    def test_null_empty_unknown_fields_do_not_override_raw_name(self):
        self.table(data={'version': 1, 'extra': 12, 'labels': {MD5:
            {'text': None, 'name': ' \n ', 'album': None, 'text_model': [], 'text_prompt': {}}}})
        with self.assertLogs('wechat_diary_core.emoji', level='WARNING'):
            self.assertIn('：〔原名〕\n', self.final())
        self.assertIn(MD5, self.config.emoji_labels.store.labels)

    def test_config_relative_path_cache_reads_once_across_files(self):
        self.table({'text': '缓存转写'})
        self.assertEqual(self.config.emoji_labels.path, self.path)
        original = Path.read_text
        reads = []
        def read(path, *args, **kwargs):
            if path == self.path:
                reads.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            for _ in range(3):
                self.assertIn('：〔缓存转写〕\n', self.final())
        self.assertEqual(len(reads), 1)
        self.table({'text': '下一轮'})
        self.assertIn('：〔缓存转写〕\n', self.final())
        self.assertIn('：〔下一轮〕\n', self.final(config=load_config(self.root / 'config.toml')))

    def test_disabled_matches_first_round_bytes(self):
        # This golden is captured from the untouched a513b41 worktree at task start.
        golden = (ROOT / 'tests/fixtures/emoji_labels_disabled.md').read_bytes()
        for stanza in ('', '[emoji_labels]\npath=""\n'):
            config = self.root / 'off.toml'
            config.write_text(stanza, encoding='utf-8')
            cfg = load_config(config)
            with self.assertNoLogs('wechat_diary_core.emoji'):
                self.final(config=cfg)
            self.assertEqual(self.output.read_bytes(), golden)
            self.assertIsNone(cfg.emoji_labels.path)

    def test_doctor_sees_table_errors_and_ready_state_offline(self):
        spec = importlib.util.spec_from_file_location('emoji_doctor_test', ROOT / 'scripts/doctor.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.addCleanup(sys.modules.pop, spec.name)
        for entry in (None, {'text': '转写'}):
            cfg_path = self.root / 'config.toml'
            cfg_path.write_text('[export_backend]\nbackend="manual"\n[emoji_labels]\npath="labels.json"\n', encoding='utf-8')
            if entry:
                self.table(entry)
            report = module.run_doctor(cfg_path, deps=module.ProbeDependencies(find_spec=lambda _: object(), can_write=lambda _: True))
            check = next(c for c in report.checks if c.id == 'emoji_labels')
            self.assertEqual(check.status, 'ready' if entry else 'warning')
            self.assertIn('path', check.details)
            self.assertEqual(check.details['entries'], 1 if entry else 0)
            if not entry:
                self.assertIn('导出可继续', check.action)


if __name__ == '__main__':
    unittest.main()
