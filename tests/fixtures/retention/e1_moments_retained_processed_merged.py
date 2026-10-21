"""e1: retained daily Moments with a pre-existing partial aggregate.
Renderer configuration and local-noon date corrected by the dispatch.
The original script remains immutable in the private evidence directory.
"""
from common import *  # noqa
from datetime import datetime
from wechat_diary_core.config import load_config
from wechat_diary_core.workspace import rotate_export_workspace

root = fresh('e1')
cfg = config(root)
# Approved synthetic-premise correction: this renderer really owns the seeded path.
config_path = root / 'config.toml'
config_path.write_text(config_path.read_text(encoding='utf-8') + '\n[daily_export]\ntarget_usernames=["wxid_synthetic_self"]\ntarget_processed_subroot="_self"\n', encoding='utf-8')
cfg = load_config(config_path)
archive = root / 'archived' / 'raw'
name = '朋友圈导出_20300102_abcdef12.json'
media_rel = '_media/images/ab/' + hashlib.md5(b'img').hexdigest() + '.jpg'


def moments(posts, export_time):
    return {'exportTime': export_time, 'filters': {'usernames': ['wxid_synthetic_self']},
            'posts': [{'id': p, 'username': 'wxid_synthetic_self', 'createTime': int(datetime(2030, 1, 2, 12).timestamp()),
                       'contentDesc': f'post {p}', 'media': [{'localPath': media_rel}]} for p in posts]}

old = save(archive / name, moments(['p1', 'p2', 'p3'], 1))
old_bytes = old.read_bytes()
archived_md = save(root / 'archived' / 'processed' / '_self' / 'moments' / '2030-01-02.md', b'ARCHIVED: p1 p2 p3\n')
incoming = save(root / 'live' / name, moments(['p1', 'p2'], 2))  # e.g. same-day rerun after a deletion
save(root / 'live' / media_rel, b'img')
live_md = save(root / 'processed' / '_self' / 'moments' / '2030-01-02.md', b'INCOMING: p1 p2\n')

rotate_export_workspace(cfg, report_opener=None)
print('archived moments raw unchanged:', old.read_bytes() == old_bytes)
print('incoming moments retained in live raw:', incoming.exists())
print('its media still in live raw:', (root / 'live' / media_rel).exists())
print('archived processed md =', archived_md.read_bytes())
print('VIOLATION (retained incoming day render merged)' if archived_md.read_bytes() != b'ARCHIVED: p1 p2 p3\n' else 'ok')

print('live moments md still exists:', live_md.exists())
print('report lists path:', '_self/moments/2030-01-02.md' in (root / 'archived/raw-fold-report.md').read_text(encoding='utf-8'))
