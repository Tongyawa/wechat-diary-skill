"""B1: process_existing_raw (also = run_daily_export manual backend path) re-render.
A retained group chat 'Alpha' (other wxid at the archive's Alpha) in which the target speaks protects
the target sidecar day file _targets/chats/D.md.  The target's own private chat for D is READY (not
retained) and gets re-exported with one more message.  Re-render must show the new message in the
sidecar; the protection skip leaves the stale sidecar in place (and later merges it to the archive).
"""
from common import *  # noqa
from wechat_diary_core.config import load_config
import process_existing_raw as per

root = fresh('b1-' + os.environ.get('TAG','x'))
(root / 'config.toml').write_text('[paths]\nraw="live"\nprocessed="processed"\narchived="archived"\ninsights="insights"\n'
    '[daily_export]\ntarget_usernames=["wxid_target_t"]\nself_moments_usernames=[]\n', encoding='utf-8')
cfg = load_config(root / 'config.toml')
archive = root / 'archived' / 'raw'
save(archive / 'Alpha' / 'Alpha_20300102.json', chat([1, 2, 3], wxid='wxid_victim_w1'))
grp = chat([1, 2], wxid='grp_other@chatroom', name='Alpha')
grp['session']['type'] = '群聊'
for m in grp['messages']:
    m['senderUsername'] = 'wxid_target_t'
save(root / 'live' / 'Alpha_20300102' / 'Alpha_20300102.json', grp)
tgt = chat([1, 2], wxid='wxid_target_t', name='Target')
tpath = save(root / 'live' / 'Target_20300102' / 'Target_20300102.json', tgt)

per.process_existing_raw(cfg, skip_voice_fallback=True)
side = root / 'processed' / '_targets' / 'chats' / '2030-01-02.md'
print('run1 sidecar has text-2:', 'text-2' in side.read_text(encoding='utf-8'))
tgt2 = chat([1, 2, 3], wxid='wxid_target_t', name='Target')
tgt2['messages'][2]['content'] = 'NEW-MSG-AFTER-REEXPORT'
save(tpath, tgt2)
per.process_existing_raw(cfg, skip_voice_fallback=True)
own = root / 'processed' / 'Target' / '2030-01-02.md'
print('run2 Target/D.md has NEW-MSG:', own.is_file() and 'NEW-MSG-AFTER-REEXPORT' in own.read_text(encoding='utf-8'))
print('run2 sidecar D.md has NEW-MSG:', 'NEW-MSG-AFTER-REEXPORT' in side.read_text(encoding='utf-8'))
arch_side = root / 'archived' / 'processed' / '_targets' / 'chats' / '2030-01-02.md'
print('archived sidecar exists:', arch_side.exists())
