"""Schema-invalid raw remains unarchived. Processed behavior matches main:
plain and move reject the collision; explicit force can replace the Markdown.
The closed snapshot contract accepts that existing main behavior.
"""
from common import *  # noqa
import archive_exports

variant = sys.argv[1:] 
root = fresh('a1-' + ('-'.join(variant) or 'plain'))
cfg = config(root)
archive = root / 'archived' / 'raw'
victim = chat([1, 2, 3, 4, 5], wxid='wxid_victim_w1')
save(archive / 'Alpha' / 'Alpha_20300102.json', victim)
victim_md = save(root / 'archived' / 'processed' / 'Alpha' / '2030-01-02.md', b'VICTIM W1 RENDER\n')
bad = chat([1, 2], wxid='wxid_other_w2')
bad['messages'][0]['createTime'], bad['messages'][1]['createTime'] = 2000, 1000  # order violation
incoming = save(root / 'recovered' / 'Alpha_20300102' / 'Alpha_20300102.json', bad)
live_md = save(root / 'recproc' / 'Alpha' / '2030-01-02.md', b'OTHER W2 RENDER (from unarchived raw)\n')
argv = ['--config', str(root / 'config.toml'), '--raw-root', str(root / 'recovered'), '--processed-root', str(root / 'recproc')]
if 'force' in variant: argv.append('--force-overwrite')
if 'move' in variant: argv.append('--move-source')
rc = archive_exports.main(argv)
print('rc =', rc)
print('incoming raw still unarchived in raw root:', incoming.exists(), '| archive raw has victim only:',
      sorted(p.relative_to(archive).as_posix() for p in archive.rglob('*.json')))
print('archived md =', victim_md.read_bytes())
print('processed overwritten under explicit force' if victim_md.read_bytes() != b'VICTIM W1 RENDER\n' else 'ok')
