"""S1 (Fable 10-19): with card-3 raw retention but main-style unconditional processed merge
(no exclusion at render, no protection), a retained group chat left in live raw re-renders
day N's target sidecar on day N+1 from the retained input alone, and the next merge overwrites
the archive's full sidecar N with the partial one.  Arg 'exclude' = render day N+1 with the
retained raw excluded (simulated by moving it aside during render) -> archive sidecar intact."""
import sys, os
from common import *  # noqa
from wechat_diary_core.config import load_config
from wechat_diary_core.workspace import merge_raw_exports_into_archive, merge_processed_into_archive
from wechat_diary_core.archiving import archive_chats_for, archive
mode = sys.argv[1] if len(sys.argv) > 1 else 'main'
root = fresh('s1-' + mode)
(root / 'config.toml').write_text('[paths]\nraw="live"\nprocessed="processed"\narchived="archived"\ninsights="insights"\n'
    '[daily_export]\ntarget_usernames=["wxid_target_t"]\n', encoding='utf-8')
cfg = load_config(root / 'config.toml')
A = root / 'archived' / 'raw'
save(A / 'Alpha' / 'Alpha_20300102.json', chat([1, 2, 3], wxid='wxid_victim_w1'))
grp = chat([7, 8], wxid='grp_other@chatroom', name='Alpha'); grp['session']['type'] = '群聊'
for m in grp['messages']: m['senderUsername'] = 'wxid_target_t'
grp['messages'].append(dict(grp['messages'][-1], localId=9, createTime=1009, content='SELF-IN-GROUP', isSend=1, senderUsername='wxid_self_me', platformMessageId='pm-9'))
gpath = save(root / 'live' / 'Alpha_20300102' / 'Alpha_20300102.json', grp)
t = chat([1, 2], wxid='wxid_target_t', name='Target'); t['messages'][1]['content'] = 'PRIVATE-DAY-N-MSG'
save(root / 'live' / 'Target_20300102' / 'Target_20300102.json', t)

def render(excluded=None):
    archive(cfg.paths.raw, config=cfg, clear_first=True, excluded_sources=excluded)
    archive_chats_for(['wxid_target_t'], config=cfg, subroot='_targets/chats', image_mode='preserve_paths', clear_first=True, excluded_sources=excluded)
def rotate():
    retained = []
    merge_raw_exports_into_archive(cfg.paths.raw, A, report_opener=None, retained_inputs=retained)
    merge_processed_into_archive(cfg.paths.processed, root / 'archived' / 'processed', excluded_sources=set())  # main-style: no merge-time retention decision
    return retained

render()                                   # day N render: sidecar N = group + private
side_arch = root / 'archived' / 'processed' / '_targets' / 'chats' / '2030-01-02.md'
retained = rotate()                        # day N+1 rotation: Target archived, Alpha retained
print('retained after N+1 rotation:', [p.name for p in retained], '| still in live:', gpath.exists())
print('archive sidecar N has private msg after N+1 merge:', 'PRIVATE-DAY-N-MSG' in side_arch.read_text(encoding='utf-8'))
t2 = chat([1], wxid='wxid_target_t', name='Target', day='2030-01-03')
save(root / 'live' / 'Target_20300103' / 'Target_20300103.json', t2)   # day N+1 export
render(retained if mode == 'exclude' else set())
live_side_N = root / 'processed' / '_targets' / 'chats' / '2030-01-02.md'
print('live sidecar N re-rendered on day N+1:', live_side_N.exists(),
      '| contains private msg:', live_side_N.exists() and 'PRIVATE-DAY-N-MSG' in live_side_N.read_text(encoding='utf-8'))
rotate()                                   # day N+2 rotation merges day N+1 processed
print('archive sidecar N still has private msg after N+2 merge:', 'PRIVATE-DAY-N-MSG' in side_arch.read_text(encoding='utf-8'))
