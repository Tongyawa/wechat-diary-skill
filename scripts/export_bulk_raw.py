from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, TextIO


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_on_demand import SessionSelectionError, resolve_session
from wechat_diary_core.backends.weflow_api.backend import BulkExportResult, WeflowApiBackend
from wechat_diary_core.backends.weflow_api.failure_state import normalize_error_text
from wechat_diary_core.config import load_config
from wechat_diary_core.workspace_discovery import WorkspaceResolutionError, resolve_config_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="批量导出全部或指定 WeFlow 会话的日期范围，只产出按天切分的 canonical raw。"
    )
    parser.add_argument("--start", help="起始日期，接受 yyyy-mm-dd 或 yyyymmdd；与 --end 同给同省。")
    parser.add_argument("--end", help="结束日期，接受 yyyy-mm-dd 或 yyyymmdd；与 --start 同给同省。")
    parser.add_argument("--out", required=True, help="canonical raw 的显式输出根。")
    parser.add_argument(
        "--session",
        action="append",
        default=[],
        help="会话 wxid 或显示名子串；可重复。省略表示全部会话。",
    )
    parser.add_argument("--force", action="store_true", help="忽略完成区间状态并重导本次范围。")
    parser.add_argument(
        "--chunk-target",
        type=int,
        default=10_000,
        help="每个导出块的目标消息数；单日超过该值时仍保持单日一个块（默认 10000）。",
    )
    parser.add_argument(
        "--replan",
        action="store_true",
        help="强制重新以 media=false 扫描本次范围的逐日消息数；不等同于重导。",
    )
    parser.add_argument(
        "--expand-range",
        action="store_true",
        help="显式允许把既有 plan 区间向前或向后扩展；不允许缩小。",
    )
    parser.add_argument(
        "--retry-no-local-records",
        action="store_true",
        help="重新尝试 plan 中已判定为本机无记录的会话。",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="把 driver 的 stdout/stderr 逐行加时间戳后追加写入 UTF-8 日志。",
    )
    parser.add_argument("--config", default=None, help="配置文件路径。")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    try:
        with _driver_log(_log_value_from_argv(raw_argv)):
            parser = build_parser()
            args = parser.parse_args(raw_argv)
            return _run(parser, args)
    except OSError as exc:
        print(f"批量 raw 导出失败：无法写入 --log：{exc}", file=sys.stderr)
        return 1


def _run(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    backend: WeflowApiBackend | None = None
    selected: list[dict[str, Any]] = []
    try:
        if (args.start is None) != (args.end is None):
            parser.error("--start 与 --end 必须同时提供或同时省略。")
        start = _parse_day(args.start) if args.start is not None else None
        end = _parse_day(args.end) if args.end is not None else None
        if start is not None and end is not None and start > end:
            parser.error("--start 不能晚于 --end。")
        if args.chunk_target <= 0:
            parser.error("--chunk-target 必须大于 0。")
        raw_root = _resolve_output_path(args.out)
        state_path = bulk_state_path_for(raw_root)
        plan_path = bulk_plan_path_for(raw_root)
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
            plan_path=plan_path,
            chunk_target=args.chunk_target,
            replan=args.replan,
            expand_range=args.expand_range,
            retry_no_local_records=args.retry_no_local_records,
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
    print(f"逐日 plan：{plan_path}")
    if result.reconciliation_path is not None:
        print(
            f"对账：{result.reconciliation_path}"
            f"（差异日 {result.reconciliation_differences}；"
            f"覆盖缺口 {result.reconciliation_incomplete_coverage_days}）"
        )
    print(
        "汇总："
        f"会话 {result.requested_sessions}；"
        f"本轮 plan {result.planned_sessions}；"
        f"复用 plan {result.resumed_plans}；"
        f"完成区间 {result.completed_intervals}；"
        f"续跑跳过 {result.resumed_intervals}；"
        f"消息 {result.messages_written}；"
        f"文件 {len(result.files_written)}；"
        f"失败 {len(result.failures)}；"
        f"本机无记录 {result.no_local_records}；"
        f"群名册降级 {result.group_roster_failures}/{result.group_roster_attempts}"
    )
    if result.failures:
        _print_failure_groups(result.failures, result.session_display_names)
    if result.stopped_reason is not None:
        print(f"批量 raw 导出停止：{result.stopped_reason}", file=sys.stderr)
        return 1
    if result.failures:
        print("批量导出未全部完成；修复失败原因后原命令重跑即可续跑。", file=sys.stderr)
        return 1
    if result.reconciliation_incomplete_coverage_days:
        print(
            "批量导出覆盖不完整；请按续跑状态用原命令重跑，"
            "直至对账覆盖缺口归零。",
            file=sys.stderr,
        )
        return 1
    return 0


def _print_failure_groups(
    failures: Mapping[str, str], session_display_names: Mapping[str, str]
) -> None:
    groups: dict[str, list[str]] = {}
    for talker, reason in failures.items():
        fingerprint = normalize_error_text(reason)
        groups.setdefault(fingerprint, []).append(session_display_names.get(talker, talker))
    print("失败清单（按指纹分组）：", file=sys.stderr)
    for fingerprint, display_names in sorted(groups.items()):
        print(f"  - 指纹（{len(display_names)} 个）：{fingerprint}", file=sys.stderr)
        print(f"    会话：{'、'.join(sorted(display_names))}", file=sys.stderr)


class _TimestampedLogTee:
    def __init__(self, console: TextIO, log: TextIO) -> None:
        self.console = console
        self.log = log
        self.pending = ""

    def write(self, value: str) -> int:
        written = self.console.write(value)
        self.pending += value
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line.endswith("\r"):
                line = line[:-1]
            self.log.write(f"[{datetime.now():%H:%M:%S}] {line}\n")
        return written

    def flush(self) -> None:
        self.console.flush()
        self.log.flush()

    def finish(self) -> None:
        if self.pending:
            self.log.write(f"[{datetime.now():%H:%M:%S}] {self.pending}\n")
            self.pending = ""
        self.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.console, name)


@contextmanager
def _driver_log(value: str | None) -> Iterator[None]:
    if not value:
        yield
        return
    path = _resolve_output_path(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as log:
        original_stdout, original_stderr = sys.stdout, sys.stderr
        stdout_tee = _TimestampedLogTee(original_stdout, log)
        stderr_tee = _TimestampedLogTee(original_stderr, log)
        sys.stdout, sys.stderr = stdout_tee, stderr_tee
        try:
            yield
        finally:
            stdout_tee.finish()
            stderr_tee.finish()
            sys.stdout, sys.stderr = original_stdout, original_stderr


def _log_value_from_argv(argv: list[str]) -> str | None:
    value: str | None = None
    for index, token in enumerate(argv):
        if token.startswith("--log="):
            value = token.partition("=")[2]
        elif token == "--log" and index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            value = argv[index + 1]
    return value


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


def bulk_plan_path_for(raw_root: Path) -> Path:
    return raw_root.parent / f".{raw_root.name}.bulk-export-plan.json"


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
