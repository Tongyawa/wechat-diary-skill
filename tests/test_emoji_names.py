from __future__ import annotations

import base64
import copy
from datetime import date
import io
import itertools
import json
from pathlib import Path
import tempfile
import unittest

from wechat_diary_core.archiving import archive
from wechat_diary_core.backends.weflow_api.client import WeflowApiClient
from wechat_diary_core.backends.weflow_api.emoji import decode_desc, emoji_metadata
from wechat_diary_core.backends.weflow_api.mapper import map_session_json, write_session_export
from wechat_diary_core.chat_flow import render_chat_flow, render_message_content
from wechat_diary_core.config import load_config
from wechat_diary_core.emoji import render_emoji
from wechat_diary_core.preprocessing.cleaner import clean_messages
from wechat_diary_core.preprocessing.time_compress import compress_nearby_messages
from wechat_diary_core.preprocessing.vision_units import ImageItem, chat_body
from wechat_diary_core.raw_schema import RawSchemaError, validate_session_json


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/weflow_emoji_names.json').read_text(encoding='utf-8'))
SESSION = {'username': 'contact_placeholder', 'displayName': '示例联系人'}


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result) + bytes([value])


def field(number, value):
    return varint(number << 3 | 2) + varint(len(value)) + value


def desc(*entries):
    # Observed repeated field 1 containing language=1, text=2.
    return base64.b64encode(b''.join(field(1, field(1, lang.encode()) + field(2, text.encode()))
                                   for lang, text in entries)).decode()


def api_emoji(value):
    message = copy.deepcopy(FIXTURE['messages'][0])
    original = 'ChQKB2RlZmF1bHQSCeekuuS+i+WQjQ=='
    message['rawContent'] = message['rawContent'].replace(original, value)
    return message


def mapped(messages):
    return map_session_json(SESSION, messages, contacts=[], require_media=False)


class EmojiNameTests(unittest.TestCase):
    def test_observed_shapes_and_sender_prefix(self):
        named = FIXTURE['messages'][0]
        self.assertEqual(emoji_metadata(named['rawContent']).get('emojiName'), '示例名')
        self.assertEqual(emoji_metadata(FIXTURE['zh_cn_only']).get('emojiName'), '示例名')
        self.assertEqual(emoji_metadata('sender_placeholder:\n' + named['rawContent']).get('emojiName'), '示例名')
        self.assertEqual(emoji_metadata(FIXTURE['messages'][1]['rawContent']), {})

    def test_language_priority_and_empty_entries(self):
        cases = [((('default', '默认名'), ('zh_cn', '简体名'), ('zh_tw', '繁体名')), '简体名'),
                 ((('zh_cn', '  '), ('default', '默认名'), ('zh_tw', '繁体名')), '默认名'),
                 ((('default', ''), ('zh_tw', '繁体名')), '繁体名'),
                 ((('en', 'Sample'),), 'Sample'),
                 ((('zh_cn', ''), ('zh_cn', '示例名')), '示例名')]
        for entries, expected in cases:
            with self.subTest(entries=entries):
                self.assertEqual(decode_desc(desc(*entries)), expected)

    def test_malformed_desc_degrades_all_the_way_to_processed(self):
        valid = desc(('default', '示例名'))
        body = base64.b64decode(valid)
        broken = ['', '%%%invalid%%%', valid + '!', base64.b64encode(body[:-1]).decode(),
                  base64.b64encode(b'\x0a\xff\x01' + body[2:]).decode(),
                  base64.b64encode(body + b'\x80').decode(),
                  base64.b64encode(b'\x00').decode(),
                  base64.b64encode(field(1, field(1, b'default') + field(2, b'\xff'))).decode(),
                  base64.b64encode(b'\xff' * 12).decode(),
                  base64.b64encode(b'\x0b').decode(), 'A' * 8193]
        for value in broken:
            with self.subTest(value=value[:35]):
                data = mapped([api_emoji(value)])
                self.assertNotIn('emojiName', data['messages'][0])
                self.assertEqual(render_message_content(data['messages'][0]), '[表情]')
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    source = root / 'raw.json'
                    source.write_text(json.dumps(data), encoding='utf-8')
                    files = archive(root / 'raw.json', config=load_config(root / 'missing.toml'),
                                    output_root=root / 'processed', clear_first=False, image_mode='preserve_paths')
                    self.assertEqual(len(files), 1)
                    self.assertIn('：[表情]\n', files[0].read_text(encoding='utf-8'))

    def test_xml_bounds_unknown_fields_and_no_emojiattr_fallback(self):
        for value in (None, 42, '<msg>', '<msg>' + 'x' * 65537, '<!DOCTYPE msg><msg/>',
                      '<msg><emoji emojiattr="ChQKB2RlZmF1bHQSCeekuuS+i+WQjQ=="/></msg>'):
            self.assertEqual(emoji_metadata(value), {})
        payload = base64.b64decode(desc(('default', '示例名')))
        unknown = b'\x10\x01\x19' + b'0' * 8 + b'\x25' + b'0' * 4 + field(9, b'unknown')
        self.assertEqual(decode_desc(base64.b64encode(payload + unknown).decode()), '示例名')

    def test_names_are_one_bounded_segment(self):
        name = '示例\n名 | 〔补充〕\r\n\x00末尾'
        message = mapped([api_emoji(desc(('zh_cn', name)))])['messages'][0]
        self.assertEqual(message.get('emojiName'), name)
        self.assertEqual(render_message_content(message), '〔示例 名 ｜ （补充） 末尾〕')
        self.assertEqual(render_emoji({'emojiName': '长' * 1000}), '〔' + '长' * 79 + '…〕')
        for empty in (None, 123, '', '\n\x00\u202e'):
            self.assertEqual(render_emoji({'emojiName': empty}), '[表情]')

    def test_api_to_raw_cleaner_vision_and_processed_file(self):
        payload = json.dumps({'messages': FIXTURE['messages'], 'count': 3, 'hasMore': False}).encode()
        client = WeflowApiClient('http://placeholder', 'placeholder', opener=lambda *a, **k: io.BytesIO(payload))
        messages = client.fetch_messages('contact_placeholder', start='20261104', end='20261104', media=True)
        self.assertEqual(messages[0]['rawContent'], FIXTURE['messages'][0]['rawContent'])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = write_session_export(root / 'raw', SESSION, messages, start=date(2026, 11, 4),
                                       end=date(2026, 11, 4), contacts=[], require_media=False)
            data = json.loads(next(raw.glob('*.json')).read_text(encoding='utf-8'))
            self.assertEqual([m['localId'] for m in data['messages']], [1, 2, 3])
            self.assertEqual(data['messages'][2].get('emojiName'), '示例名')
            self.assertEqual(data['messages'][2].get('emojiDesc'), 'ChQKB2RlZmF1bHQSCeekuuS+i+WQjQ==')
            cfg = load_config(root / 'missing.toml')
            cleaned = clean_messages(data['messages'], cfg.preprocessing)
            self.assertEqual(cleaned[2]['content'], '〔示例名〕')
            self.assertEqual(cleaned[1]['content'], '[表情]')
            # This independent consumer sees the same canonical name, before compression.
            image = dict(cleaned[0], type='图片消息', content='[图片]')
            context_messages = [cleaned[2], image]
            item = ImageItem(Path('unused'), image, 1, 1, image['createTime'],
                             image['senderUsername'], image['formattedTime'][:10], 'sha', 'md5', (1, 1), None)
            self.assertIn('〔示例名〕', chat_body(context_messages, [item], cfg.preprocessing.image_vision))
            files = archive(root / 'raw', config=cfg, output_root=root / 'processed', clear_first=False,
                            image_mode='preserve_paths')
            self.assertEqual(len(files), 1)
            self.assertIn('：示例正文 | [表情] | 〔示例名〕\n', files[0].read_text(encoding='utf-8'))

    def test_emoji_with_quote_survives_merge(self):
        emoji = api_emoji(desc(('default', '示例名')))
        emoji['quote'] = {'content': '被引示例', 'accountName': '示例作者', 'sender': 'author_placeholder'}
        data = mapped([FIXTURE['messages'][2], emoji])
        result = compress_nearby_messages(data['messages'])
        self.assertEqual(len(result), 1)
        self.assertIn('示例正文 | 〔示例名〕[引用 示例作者：被引示例]', render_chat_flow(result))

    def test_emoji_fields_are_optional_strings_and_legacy_still_works(self):
        data = mapped([FIXTURE['messages'][1]])
        validate_session_json(data)
        self.assertEqual(render_message_content(data['messages'][0]), '[表情]')
        for key in ('emojiName', 'emojiDesc'):
            for invalid in (None, 1, [], {}):
                broken = copy.deepcopy(data)
                broken['messages'][0][key] = invalid
                with self.subTest(key=key, invalid=invalid), self.assertRaises(RawSchemaError):
                    validate_session_json(broken)


class EmojiMergeTests(unittest.TestCase):
    def message(self, kind, index):
        return {'type': kind, 'content': '[表情]' if kind == '动画表情' else '示例正文',
                'emojiName': '示例名' if kind == '动画表情' else '', 'isSend': 0,
                'senderUsername': 'contact_placeholder', 'createTime': 1000 + index,
                'localId': index, 'platformMessageId': f'p{index}'}

    def test_each_mergeable_order_keeps_segments_and_all_identities(self):
        for kinds in itertools.product(('文本消息', '引用消息', '动画表情'), repeat=3):
            messages = [self.message(kind, i) for i, kind in enumerate(kinds, 1)]
            result = compress_nearby_messages(messages)
            with self.subTest(kinds=kinds):
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]['compressed_local_ids'], [1, 2, 3])
                self.assertEqual(result[0]['compressed_platform_message_ids'], ['p1', 'p2', 'p3'])
                self.assertEqual([m['type'] for m in result[0]['compressed_segments']], list(kinds))
                self.assertEqual(render_message_content(result[0]), ' | '.join(render_message_content(m) for m in messages))
                self.assertNotIn('compressed_segments', messages[0])

    def test_sender_direction_and_time_boundaries_still_split(self):
        changes = [{'senderUsername': 'other_placeholder'}, {'isSend': 1}, {'createTime': 999}, {'createTime': 1122}]
        for kinds in (('文本消息', '动画表情'), ('动画表情', '文本消息')):
            for change in changes:
                left, right = self.message(kinds[0], 1), self.message(kinds[1], 2)
                right.update(change)
                with self.subTest(kinds=kinds, change=change):
                    self.assertEqual(len(compress_nearby_messages([left, right], max_interval_sec=120)), 2)
            right = self.message(kinds[1], 2)
            right['createTime'] = 1121
            self.assertEqual(len(compress_nearby_messages([self.message(kinds[0], 1), right])), 1)

    def test_other_message_types_remain_barriers(self):
        for kind in ('图片消息', '语音消息', '视频消息', '其他消息'):
            messages = [self.message('动画表情', 1), self.message(kind, 2), self.message('文本消息', 3)]
            with self.subTest(kind=kind):
                self.assertEqual(len(compress_nearby_messages(messages)), 3)

    def test_pinned_text_after_sticker_retains_marker(self):
        messages = [self.message('动画表情', 1), self.message('文本消息', 2)]
        messages[1]['is_chatroom_top_message'] = True
        result = compress_nearby_messages(messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['compressed_local_ids'], [1, 2])
        self.assertIn('【置顶消息】：〔示例名〕 | 示例正文', render_chat_flow(result))


if __name__ == '__main__':
    unittest.main()
