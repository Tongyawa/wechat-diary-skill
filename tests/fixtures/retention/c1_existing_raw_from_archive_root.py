"""C1: process_existing_raw --raw-root <a folder inside archived/raw> (re-render historical raw).
Live processed holds yesterday's ordinary renders (their raw already archived, nothing retained).
Expected (before): old renders merged into archived/processed, processed root then holds only the
re-rendered history.  Repair: replanning [source] against the archive raises (overlap) -> fail closed
-> every processed file protected -> nothing merged, clear_first suppressed, old renders stay mixed in.
"""
from common import *  # noqa
from wechat_diary_core.config import load_config
import process_existing_raw as per

root = fresh('c1-' + os.environ.get('TAG', 'x'))
cfg = config(root)
archive = root / 'archived' / 'raw'
save(archive / 'Beta' / 'Beta_20290505.json', chat([1, 2], wxid='wxid_beta', day='2029-05-05'))
save(archive / 'Gamma' / 'Gamma_20300102.json', chat([1, 2], wxid='wxid_gamma'))  # yesterday, already archived
old = save(root / 'processed' / 'Gamma' / '2030-01-02.md', b'GAMMA RENDER (raw already archived)\n')
(root / 'live').mkdir()
per.process_existing_raw(cfg, raw_root=archive / 'Beta', skip_voice_fallback=True, day='2029-05-05')
print('processed files after:', sorted(p.relative_to(root / 'processed').as_posix() for p in (root / 'processed').rglob('*.md')))
print('Gamma render merged to archive:', (root / 'archived' / 'processed' / 'Gamma' / '2030-01-02.md').exists())
