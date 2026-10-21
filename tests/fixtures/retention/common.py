"""Shared synthetic-fixture helpers. Independent of production path resolution."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

WT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WT))
sys.path.insert(0, str(WT / 'scripts'))
HERE = Path(__file__).resolve().parent


def sha(b):
    return hashlib.sha256(b).hexdigest()


def chat(ids, *, wxid='wxid_synthetic_a', day='2030-01-02', name='Synthetic'):
    session = dict(wxid=wxid, username=wxid, displayName=name, nickname=name,
                   remark='', type='私聊', messageCount=len(ids))
    messages = []
    for n in ids:
        messages.append(dict(localId=n, createTime=1000 + n, formattedTime=f'{day} 10:00:00',
                             type='文本消息', content=f'text-{n}', source='', isSend=0,
                             senderUsername='wxid_synthetic_sender', senderDisplayName='S',
                             senderRemark='', senderNickname='S', platformMessageId=f'pm-{n}',
                             senderGroupNickname=''))
    return dict(session=session, messages=messages,
                weflow=dict(source='http_api', mediaScheme='cas-v1'))


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
    path.write_bytes(raw)
    return path


def fresh(name):
    root = HERE / 'runs' / name
    if not root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise RuntimeError('Run through unittest with a system-temp fixture root')
    root.mkdir(parents=True)
    return root


def snapshot(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()} if root.exists() else {}


def folded_shas(archived_parent):
    out = set()
    for p in (Path(archived_parent) / 'raw-folded').rglob('*'):
        if p.is_file() and p.name != 'manifest.json':
            out.add(sha(p.read_bytes()))
    return out


def config(root):
    from wechat_diary_core.config import load_config
    path = Path(root) / 'config.toml'
    path.write_text('[paths]\nraw="live"\nprocessed="processed"\narchived="archived"\ninsights="insights"\n', encoding='utf-8')
    return load_config(path)
