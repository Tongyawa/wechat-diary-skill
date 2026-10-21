"""P1: cost of the new pre-merge replanning in process_existing_raw / manual backend (no header cache).
Synthetic archive N sessions x M days; one live raw file; one processed file."""
from common import *  # noqa
import time
from wechat_diary_core.workspace import retained_raw_sources
root = fresh('p1')
cfg = config(root)
N, M = int(sys.argv[1]), int(sys.argv[2])
for s in range(N):
    for d in range(M):
        day = f'2029{(d // 28) + 1:02d}{(d % 28) + 1:02d}'
        save(root / 'archived' / 'raw' / f'S{s}' / f'S{s}_{day}.json', chat([1, 2, 3], wxid=f'wxid_s{s}', day=f'{day[:4]}-{day[4:6]}-{day[6:]}'))
save(root / 'live' / 'S0_20300102' / 'S0_20300102.json', chat([1], wxid='wxid_s0'))
save(root / 'processed' / 'S0' / '2030-01-02.md', b'x')
t = time.perf_counter()
retained_raw_sources(cfg.paths.archived, live_raw_root=cfg.paths.raw)
elapsed = time.perf_counter()-t
print(f'archive files={N*M} retained_raw_sources seconds={time.perf_counter()-t:.2f}')
