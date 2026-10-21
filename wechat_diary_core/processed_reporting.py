"""Processed-impact Markdown; no changes to the raw machine ledger."""
from pathlib import Path
from . import raw_archive

SECTION = '## processed 影响'
GROUPS = ('1:1 会话文件：已跳过', '多对一日文件：已跳过，归档保留旧版', '归档无同路径：已并入')
PROCESSED_GUIDANCE = (
    '此日归档 processed 保持旧版；需要与归档 raw 同步时手工按日重生。'
    '按日全集工具就绪前，不要用 `process_existing_raw` 单目录补 sidecar / 朋友圈日文件，'
    '它会以单会话覆盖整日。归档 processed 是历史快照，不自动重生。'
)
OLD_INSTRUCTION = 'process_existing_raw --raw-root <归档会话目录>'


def read_report_text(archived_root):
    path = Path(archived_root) / 'raw-fold-report.md'
    try:
        return path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return ''
    except Exception as exc:
        print(f'[WARN] processed 影响报告不可读取：{path}（{type(exc).__name__}）')
        return None


def _previous_rows(text):
    rows = {group: set() for group in GROUPS}
    if not text or SECTION not in text:
        return rows
    group = None
    for line in text.split(SECTION, 1)[1].splitlines():
        if line.startswith('### '):
            candidate = line[4:]
            if candidate.startswith('多对一日文件：已跳过'):
                candidate = GROUPS[1]
            group = candidate if candidate in rows else None
        elif group and line.startswith('- '):
            rows[group].add(line)
    return rows


def update_processed_report(archived_root, *, skipped_chat=(), skipped_multi=(), introduced=None, previous_text=None):
    """Preserve human-readable history; these rows never decide a later merge."""
    path = Path(archived_root) / 'raw-fold-report.md'
    try:
        current = read_report_text(archived_root)
        if current is None:
            return False
        rows = _previous_rows(current)
        previous = _previous_rows(previous_text)
        for group in GROUPS:
            rows[group].update(previous[group])
        rows[GROUPS[0]].update(f'- `{p.as_posix()}`' for p in skipped_chat)
        rows[GROUPS[1]].update(f'- `{p.as_posix()}`' for p in skipped_multi)
        for relative, sources in (introduced or {}).items():
            names = '、'.join(f'`{p}`' for p in sorted(sources))
            rows[GROUPS[2]].add(f'- `{relative.as_posix()}`：可能含被保留输入 {names} 的贡献，也可能缺其贡献；现存 Markdown 未记录生成时点，无法据此断定。')
        if not any(rows.values()):
            return False
        base = current.split(SECTION, 1)[0] or '# 归档待处理报告\n'
        base = '\n'.join(line for line in base.splitlines() if OLD_INSTRUCTION not in line).rstrip()
        lines = [base, '', SECTION, '',
                 '以下路径相对于本报告同目录的 processed/。已跳过的文件仍在 live processed 同路径。',
                 '以下为并入影响的历史记录，不参与后续轮转判定，也不触发自动重生。', '', PROCESSED_GUIDANCE, '']
        for group in GROUPS:
            if rows[group]:
                lines.extend(['### ' + group, '', *sorted(rows[group]), ''])
        text = '\n'.join(lines)
        if text == current:
            return False
        raw_archive._atomic_bytes(path, text.encode('utf-8'))
        return True
    except Exception as exc:
        print(f'[WARN] processed 影响报告未能更新：{path}（{type(exc).__name__}）')
        return False


def open_report_if_changed(archived_root, previous_text, opener):
    path = Path(archived_root) / 'raw-fold-report.md'
    current = read_report_text(archived_root)
    if opener is not None and current and current != previous_text:
        try:
            opener(path)
        except Exception:
            print(f'[WARN] 无法打开归档报告，请手动打开：{path}')


def report_raw_result(plan, result, *, report_opener=None):
    """Preserve processed history when the raw reporter refreshes its Markdown."""
    previous = read_report_text(plan.archive.parent)
    requested = []
    raw_archive.report_daily_result(plan, result, report_opener=requested.append if report_opener else None)
    update_processed_report(plan.archive.parent, previous_text=previous)
    if requested:
        open_report_if_changed(plan.archive.parent, previous, report_opener)
