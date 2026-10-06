from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping, TextIO


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_on_demand import SessionSelectionError, resolve_session
from wechat_diary_core.backends.weflow_api.backend import BulkExportResult, WeflowApiBackend
from wechat_diary_core.backends.weflow_api.bulk_plan import BulkExportPlan
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
        "--self-moments", action="store_true",
        help="只导配置中本人的朋友圈区间为一份 raw；不进入聊天导出或续跑。",
    )
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
    parser.add_argument(
        "--print-range",
        action="store_true",
        help="完全离线打印本次实际区间的单行 JSON 后退出；不写任何导出状态。",
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
        log_value = None if "--print-range" in raw_argv else _log_value_from_argv(raw_argv)
        with _driver_log(log_value):
            parser = build_parser()
            args = parser.parse_args(raw_argv)
            chat_options = {
                "--session", "--force", "--chunk-target", "--replan",
                "--expand-range", "--retry-no-local-records",
            }
            if args.self_moments and any(
                any(option.startswith(token.partition("=")[0]) for option in chat_options)
                for token in raw_argv if token.startswith("--")
            ):
                parser.error("--self-moments 不能搭配聊天选择、规划或续跑参数。")
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
        config_path = resolve_config_path(args.config)
        cfg = load_config(config_path)
        if args.self_moments:
            if start is None or end is None:
                start, end = _configured_bulk_range(cfg)
                source = "config"
            else:
                source = "cli"
            resolution = BulkRangeResolution(
                start, end, source, None, False, start, end, False,
            )
            if args.print_range:
                _print_range_json(resolution)
                return 0
            print(f"本人朋友圈实际区间（{source}）：{start}~{end}（首尾包含）。")
            usernames = sorted({
                value.strip() for value in cfg.daily_export.self_moments_usernames
                if value.strip()
            })
            if not usernames:
                raise ValueError(
                    "请在 [daily_export].self_moments_usernames 配置本人账号；"
                    "名单为空，已拒绝导出。"
                )
            if cfg.export_backend.backend != "weflow_api":
                raise RuntimeError("本人朋友圈批量导出只支持 weflow_api 后端")
            backend = WeflowApiBackend(cfg)
            backend.prepare_bulk_export()
            moments = backend.export_moments_range(usernames, start, end, raw_root=raw_root)
            assert moments is not None
            print(f"本人朋友圈 raw：{moments.path}")
            print(
                f"汇总：动态 {moments.total_posts}；媒体发布 {moments.published_media}；"
                f"未解密/缺失媒体的动态 {moments.missing_media_posts}。"
            )
            years = "；".join(f"{year} 年 {count}" for year, count in moments.year_counts)
            print(f"按年份计数：{years or '无动态'}。")
            earliest = _moment_time(moments.earliest_timestamp)
            latest = _moment_time(moments.latest_timestamp)
            print(f"最早动态：{earliest}；最晚动态：{latest}。")
            print("统计仅覆盖本地 SNS 库已缓存动态；补刷 PC 微信本人朋友圈后可原命令重跑。")
            return 0
        state_path = bulk_state_path_for(raw_root)
        plan_path = bulk_plan_path_for(raw_root)
        plan = BulkExportPlan.load(plan_path, raw_root)
        resolution = _resolve_bulk_range(
            cfg,
            plan,
            start,
            end,
            cli_expand_range=args.expand_range,
        )
        if args.print_range:
            _print_range_json(resolution)
            return 0
        if resolution.source == "config":
            print(
                "批量 raw 区间：无 plan，使用 [bulk_export] 配置 "
                f"{resolution.start}~{resolution.end}。"
            )
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
            resolution.backend_start,
            resolution.backend_end,
            raw_root=raw_root,
            sessions=selected,
            state_path=state_path,
            force=args.force,
            plan_path=plan_path,
            chunk_target=args.chunk_target,
            replan=args.replan,
            expand_range=resolution.allow_expand,
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
        f"名单跳过 {result.skipped_excluded_sessions}；"
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


def _moment_time(timestamp: int | None) -> str:
    return "无" if timestamp is None else datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


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


def _configured_bulk_range(cfg: Any) -> tuple[date, date]:
    """Resolve the configured range for a run without explicit CLI dates."""

    try:
        start = _parse_day(cfg.bulk_export.start or "2010-01-01")
    except ValueError as exc:
        raise ValueError(f"[bulk_export].start 配置无效：{exc}") from exc

    if cfg.bulk_export.end:
        try:
            end = _parse_day(cfg.bulk_export.end)
        except ValueError as exc:
            raise ValueError(f"[bulk_export].end 配置无效：{exc}") from exc
    else:
        end = date.today() - timedelta(days=1)

    if start > end:
        raise ValueError("[bulk_export] 的 start 不能晚于 end（空 end 表示昨天）。")
    return start, end


def _range_from_existing_plan_and_config(
    cfg: Any,
    recorded_range: Any,
    configured_start: date,
    configured_end: date,
) -> tuple[date, date, bool]:
    """Merge config into a recorded range while mechanically preventing shrinkage."""

    explicit_start = cfg.bulk_export.start_configured and bool(cfg.bulk_export.start)
    explicit_end = cfg.bulk_export.end_configured and bool(cfg.bulk_export.end)
    if (explicit_start and configured_start > recorded_range.start) or (
        explicit_end and configured_end < recorded_range.end
    ):
        raise ValueError(
            f"plan 区间是 {recorded_range.start}~{recorded_range.end}；"
            f"[bulk_export] 请求 {configured_start}~{configured_end}；"
            "配置只能扩、不能缩。"
        )
    effective_start = min(recorded_range.start, configured_start)
    effective_end = max(recorded_range.end, configured_end)
    expands = (effective_start, effective_end) != (recorded_range.start, recorded_range.end)
    return effective_start, effective_end, expands


@dataclass(frozen=True)
class BulkRangeResolution:
    start: date
    end: date
    source: str
    plan_range: tuple[date, date] | None
    expands: bool
    backend_start: date | None
    backend_end: date | None
    allow_expand: bool


def _resolve_bulk_range(
    cfg: Any,
    plan: BulkExportPlan,
    cli_start: date | None,
    cli_end: date | None,
    *,
    cli_expand_range: bool,
) -> BulkRangeResolution:
    """Resolve CLI/config/plan once for both execution and ``--print-range``."""

    recorded = plan.recorded_range()
    plan_range = None if recorded is None else (recorded.start, recorded.end)
    if cli_start is not None and cli_end is not None:
        effective_start, effective_end, _ = plan.resolve_range(
            cli_start,
            cli_end,
            allow_expand=cli_expand_range,
        )
        expands = plan_range is not None and (
            effective_start,
            effective_end,
        ) != plan_range
        return BulkRangeResolution(
            start=effective_start,
            end=effective_end,
            source="cli",
            plan_range=plan_range,
            expands=expands,
            backend_start=cli_start,
            backend_end=cli_end,
            allow_expand=cli_expand_range,
        )

    configured_start, configured_end = _configured_bulk_range(cfg)
    if recorded is None:
        return BulkRangeResolution(
            start=configured_start,
            end=configured_end,
            source="config",
            plan_range=None,
            expands=False,
            backend_start=configured_start,
            backend_end=configured_end,
            allow_expand=False,
        )

    effective_start, effective_end, expands = _range_from_existing_plan_and_config(
        cfg,
        recorded,
        configured_start,
        configured_end,
    )
    return BulkRangeResolution(
        start=effective_start,
        end=effective_end,
        source="plan+config" if expands else "plan",
        plan_range=plan_range,
        expands=expands,
        backend_start=effective_start if expands else None,
        backend_end=effective_end if expands else None,
        allow_expand=expands,
    )


def _print_range_json(resolution: BulkRangeResolution) -> None:
    plan_range = (
        None
        if resolution.plan_range is None
        else {
            "start": resolution.plan_range[0].isoformat(),
            "end": resolution.plan_range[1].isoformat(),
        }
    )
    print(
        json.dumps(
            {
                "start": resolution.start.isoformat(),
                "end": resolution.end.isoformat(),
                "source": resolution.source,
                "planRange": plan_range,
                "expands": resolution.expands,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _resolve_output_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


if __name__ == "__main__":
    raise SystemExit(main())
