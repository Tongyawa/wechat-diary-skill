"""S1 at the retention boundary: ready input consumed, held input still live.

The legacy-state control supplies that historical state to the selected code's
real renderers and processed merge. It does not claim that main's raw rotation
itself produces the retained state. WT selects a real code snapshot.
"""
from common import *  # noqa
from wechat_diary_core.config import load_config
from wechat_diary_core import workspace
from wechat_diary_core.archiving import archive, archive_chats_for

mode = sys.argv[1] if len(sys.argv) > 1 else 'isolate'
root = fresh('s1-' + mode)
(root / 'config.toml').write_text('[paths]\nraw="live"\nprocessed="processed"\narchived="archived"\ninsights="insights"\n'
    '[daily_export]\ntarget_usernames=["wxid_target_t"]\n', encoding='utf-8')
cfg = load_config(root / 'config.toml')
A = cfg.paths.archived / 'raw'
save(A / 'Alpha/Alpha_20300102.json', chat([1, 2, 3], wxid='wxid_victim_w1'))
group = chat([7, 8], wxid='grp_other@chatroom', name='Alpha')
group['session']['type'] = '群聊'
for message in group['messages']:
    message['senderUsername'] = 'wxid_target_t'
group['messages'].append(dict(group['messages'][-1], localId=9, createTime=1009,
    content='SELF-IN-GROUP', isSend=1, senderUsername='wxid_self_me', platformMessageId='pm-9'))
gpath = save(cfg.paths.raw / 'Alpha_20300102/Alpha_20300102.json', group)
private = chat([1, 2], wxid='wxid_target_t', name='Target')
private['messages'][-1]['content'] = 'PRIVATE-DAY-N-MSG'
ready = save(cfg.paths.raw / 'Target_20300102/Target_20300102.json', private)

def render():
    archive(cfg.paths.raw, config=cfg, clear_first=True)
    archive_chats_for(['wxid_target_t'], config=cfg, subroot='_targets/chats',
                      image_mode='preserve_paths', clear_first=True)

render()
workspace.merge_tree(cfg.paths.processed, cfg.paths.archived / 'processed')
side_arch = cfg.paths.archived / 'processed/_targets/chats/2030-01-02.md'
assert 'PRIVATE-DAY-N-MSG' in side_arch.read_text(encoding='utf-8')
# Establish the fixed post-publication boundary without inventing a main raw guard.
save(A / 'Target/Target_20300102.json', ready.read_bytes())
ready.unlink()
if mode == 'isolate':
    workspace.merge_raw_exports_into_archive(cfg.paths.raw, A)
else:
    assert mode == 'legacy-state'
    print('control precondition: held group deliberately remains live')
print('held source remains live:', gpath.exists())
save(cfg.paths.raw / 'Target_20300103/Target_20300103.json',
     chat([1], wxid='wxid_target_t', name='Target', day='2030-01-03'))
render()
regenerated = (cfg.paths.processed / '_targets/chats/2030-01-02.md').exists()
print('live sidecar N re-rendered on day N+1:', regenerated)
workspace.merge_tree(cfg.paths.processed, cfg.paths.archived / 'processed')
preserved = 'PRIVATE-DAY-N-MSG' in side_arch.read_text(encoding='utf-8')
print('archive sidecar N still has private msg after N+2 merge:', preserved)
assert not regenerated and preserved, 'S1: retained input regenerated a partial old-day sidecar'
