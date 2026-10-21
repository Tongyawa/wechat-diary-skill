"""Same-day re-export renders the latest transcription after earlier raw isolation."""
from common import *  # noqa
from wechat_diary_core.config import load_config
from wechat_diary_core.workspace import rotate_export_workspace
from wechat_diary_core.archiving import archive_chats_for, archive

root = fresh('b2-' + os.environ.get('TAG', 'x'))
(root / 'config.toml').write_text('[paths]\nraw="live"\nprocessed="processed"\narchived="archived"\ninsights="insights"\n'
    '[daily_export]\ntarget_usernames=["wxid_target_t"]\n', encoding='utf-8')
cfg = load_config(root / 'config.toml')
archive_root = root / 'archived' / 'raw'
save(archive_root / 'Alpha' / 'Alpha_20300102.json', chat([1, 2, 3], wxid='wxid_victim_w1'))
grp = chat([7, 8], wxid='grp_other@chatroom', name='Alpha'); grp['session']['type'] = '群聊'
for m in grp['messages']:
    m['senderUsername'] = 'wxid_target_t'
save(root / 'live' / 'Alpha_20300102' / 'Alpha_20300102.json', grp)
t1 = chat([1, 2], wxid='wxid_target_t', name='Target'); t1['messages'][1]['content'] = '[语音] 转文字失败'
tpath = save(root / 'live' / 'Target_20300102' / 'Target_20300102.json', t1)

def render():
    archive(cfg.paths.raw, config=cfg, clear_first=True)
    archive_chats_for(['wxid_target_t'], config=cfg, subroot='_targets/chats', image_mode='preserve_paths', clear_first=True)

render()                                    # run N (first run of day D)
rot = rotate_export_workspace(cfg, report_opener=None)  # run N+1 rotation (same-day rerun)
t2 = chat([1, 2], wxid='wxid_target_t', name='Target'); t2['messages'][1]['content'] = 'VOICE-TRANSCRIBED-OK'
save(tpath, t2)                                  # new export of day D
render()
side = root / 'processed' / '_targets' / 'chats' / '2030-01-02.md'
print('retained:', sorted(p.relative_to(root.resolve()).as_posix() for p in getattr(rot, 'retained_sources', ())))
print('Target/D.md has transcription:', 'VOICE-TRANSCRIBED-OK' in (root / 'processed' / 'Target' / '2030-01-02.md').read_text(encoding='utf-8'))
print('sidecar D.md has transcription:', 'VOICE-TRANSCRIBED-OK' in side.read_text(encoding='utf-8'))
