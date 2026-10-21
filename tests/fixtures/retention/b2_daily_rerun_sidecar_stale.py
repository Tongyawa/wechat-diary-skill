"""B2: run_daily_export API flow, same-day rerun (e.g. rerun because voice transcription failed).
Sequence reproduced at the same call level run_daily_export uses: rotate_export_workspace ->
(new export writes live raw) -> archive_chats_for(..., clear_first=True, excluded_sources=rotation.retained_sources).
Retained: group 'Alpha' (other wxid at archive Alpha) where the target speaks -> sidecar D.md protected.
Ready: Target private chat re-exported with the transcription now present.
Then the next rotation archives Target raw (removed from live) and, once Alpha is resolved, merges the stale sidecar.
"""
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

def render(protected):
    opts = {'excluded_sources': protected} if protected else {}
    archive(cfg.paths.raw, config=cfg, clear_first=True, **opts)
    archive_chats_for(['wxid_target_t'], config=cfg, subroot='_targets/chats', image_mode='preserve_paths', clear_first=True, **opts)

render(set())                                    # run N (first run of day D)
rot = rotate_export_workspace(cfg, report_opener=None)  # run N+1 rotation (same-day rerun)
t2 = chat([1, 2], wxid='wxid_target_t', name='Target'); t2['messages'][1]['content'] = 'VOICE-TRANSCRIBED-OK'
save(tpath, t2)                                  # new export of day D
render(set(getattr(rot, 'retained_sources', ())))
side = root / 'processed' / '_targets' / 'chats' / '2030-01-02.md'
print('protected:', sorted(p.relative_to(root.resolve()).as_posix() for p in getattr(rot, 'retained_sources', ())))
print('Target/D.md has transcription:', 'VOICE-TRANSCRIBED-OK' in (root / 'processed' / 'Target' / '2030-01-02.md').read_text(encoding='utf-8'))
print('sidecar D.md has transcription:', 'VOICE-TRANSCRIBED-OK' in side.read_text(encoding='utf-8'))
