from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_on_demand import SessionSelectionError, resolve_session
from wechat_diary_core.backends.weflow_api.backend import BulkExportResult, WeflowApiBackend
from wechat_diary_core.config import load_config
from wechat_diary_core.workspace_discovery import WorkspaceResolutionError, resolve_config_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="批量导出全部或指定 WeFlow 会话的日期范围，只产出按天切分的 canonical raw。"
    )
    parser.add_argument("--start", required=True, help="起始日期，接受 yyyy-mm-dd 或 yyyymmdd。")
    parser.add_argument("--end", required=True, help="结束日期，接受 yyyy-mm-dd 或 yyyymmdd。")
    parser.add_argument("--out", required=True, help="canonical raw 的显式输出根。")
    parser.add_argument(
        "--session",
        action="append",
        default=[],
        help="会话 wxid 或显示名子串；可重复。省略表示全部会话。",
    )
    parser.add_argument("--force", action="store_true", help="忽略完成区间状态并重导本次范围。")
    parser.add_argument("--config", default=None, help="配置文件路径。")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)
    backend: WeflowApiBackend | None = None
    try:
        start = _parse_day(args.start)
        end = _parse_day(args.end)
        if start > end:
            parser.error("--start 不能晚于 --end。")
        raw_root = _resolve_output_path(args.out)
        state_path = bulk_state_path_for(raw_root)
        config_path = resolve_config_path(args.config)
        cfg = load_config(config_path)
        if cfg.export_backend.backend != "weflow_api":
            raise RuntimeError(
                "批量 raw 导出只支持 export_backend.backend = \"weflow_api\"；"
                "legacy/manual 后端不具备该能力"
            )
        backend = WeflowApiBackend(cfg)
        backend.prepare_bulk_export()
        sessions = backend.client.fetch_sessions(limit=2000)
        selected = select_sessions(sessions, args.session)
        result = backend.export_chats_range(
            start,
            end,
            raw_root=raw_root,
            sessions=selected,
            state_path=state_path,
            force=args.force,
        )
        assert isinstance(result, BulkExportResult)
    except WorkspaceResolutionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SessionSelectionError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print(f"批量 raw 导出失败：{exc}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as exc:
        print(f"批量 raw 导出失败：{exc}", file=sys.stderr)
        return 1
    finally:
        if backend is not None:
            try:
                backend.shutdown()
            except Exception as exc:
                print(f"[WARN] 批量导出收尾失败：{exc}", file=sys.stderr)

    print(f"批量 raw 输出：{raw_root}")
    print(f"续跑状态：{state_path}")
    print(
        "汇总："
        f"会话 {result.requested_sessions}；"
        f"完成区间 {result.completed_intervals}；"
        f"续跑跳过 {result.resumed_intervals}；"
        f"消息 {result.messages_written}；"
        f"文件 {len(result.files_written)}；"
        f"失败 {len(result.failures)}"
    )
    if result.failures:
        print("批量导出未全部完成；修复失败原因后原命令重跑即可续跑。", file=sys.stderr)
        return 1
    return 0


def select_sessions(
    sessions: list[dict[str, Any]],
    queries: list[str],
) -> list[dict[str, Any]]:
    if not queries:
        return sessions
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in queries:
        session = resolve_session(sessions, query)
        wxid = str(session.get("username") or "")
        if wxid not in seen:
            seen.add(wxid)
            selected.append(session)
    return selected


def bulk_state_path_for(raw_root: Path) -> Path:
    return raw_root.parent / f".{raw_root.name}.bulk-export-state.json"


def _parse_day(value: str) -> date:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"日期无效：{value!r}；应为 yyyy-mm-dd 或 yyyymmdd。") from exc


def _resolve_output_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


if __name__ == "__main__":
    raise SystemExit(main())
