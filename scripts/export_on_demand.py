from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_diary_core.archiving import archive
from wechat_diary_core.asr import SenseVoiceTranscriber
from wechat_diary_core.backends.weflow_api.client import WeflowApiClient
from wechat_diary_core.backends.weflow_api.mapper import write_session_export
from wechat_diary_core.config import Config, load_config


DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COMPACT_DAY_RE = re.compile(r"^\d{8}$")
DATE_SUFFIX_RE = re.compile(r"_(\d{8})(?:-\d{8})?$")
WINDOWS_MAX_PATH = 260
WINDOWS_PATH_WARNING_LENGTH = 240


class SessionSelectionError(RuntimeError):
    """Raised when a session cannot be selected unambiguously."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        self.exit_code = exit_code
        super().__init__(message)


@dataclass(frozen=True)
class ExportOnDemandResult:
    session: dict[str, Any]
    raw_session_dir: Path
    output_session_dir: Path
    diary_files: list[Path]
    merged_file: Path | None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按需导出一个 WeFlow 会话的指定日期范围。")
    parser.add_argument("--session", help="会话 wxid，或显示名子串。")
    parser.add_argument("--start", help="起始日期，接受 yyyy-mm-dd 或 yyyymmdd。")
    parser.add_argument("--end", help="结束日期，接受 yyyy-mm-dd 或 yyyymmdd。")
    parser.add_argument("--out", help="本次导出的输出根目录。")
    parser.add_argument("--config", default="config.toml", help="配置文件路径。")
    parser.add_argument("--group-window", action="store_true", help="开启群聊上下文窗口筛选。")
    parser.add_argument("--merged", action="store_true", help="额外产出整段合并 markdown。")
    parser.add_argument("--no-media-copy", action="store_true", help="不把媒体复制到 markdown 旁。")
    parser.add_argument("--no-asr", action="store_true", help="关闭语音转写。")
    parser.add_argument("--list-sessions", metavar="关键词", help="列出匹配候选并退出。")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = _resolve_config_path(args.config)

    try:
        cfg = load_config(config_path)
        client = _make_client(cfg)
        sessions = client.fetch_sessions(limit=2000)
        if args.list_sessions is not None:
            _print_session_candidates(sessions, args.list_sessions)
            return 0

        _require_export_arguments(parser, args)
        start = _parse_day(args.start)
        end = _parse_day(args.end)
        if start > end:
            parser.error("--start 不能晚于 --end。")
        result = export_on_demand(
            cfg,
            sessions=sessions,
            client=client,
            session_query=args.session,
            start=start,
            end=end,
            out_root=_resolve_output_path(args.out),
            group_window=args.group_window,
            merged=args.merged,
            copy_media=not args.no_media_copy,
            enable_asr=not args.no_asr,
        )
    except SessionSelectionError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print(f"\n{_format_os_error(exc)}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as exc:
        print(f"\n按需导出失败：{exc}", file=sys.stderr)
        return 1

    print(f"按需导出完成：{result.output_session_dir}")
    print(f"Raw：{result.raw_session_dir}")
    for path in result.diary_files:
        print(f"- {path}")
    if result.merged_file is not None:
        print(f"合并文件：{result.merged_file}")
    return 0


def export_on_demand(
    cfg: Config,
    *,
    sessions: list[dict[str, Any]],
    client: Any,
    session_query: str,
    start: date,
    end: date,
    out_root: str | Path,
    group_window: bool = False,
    merged: bool = False,
    copy_media: bool = True,
    enable_asr: bool = True,
    archive_fn: Callable[..., list[Path]] | None = None,
    transcriber_factory: Callable[..., Any] | None = None,
) -> ExportOnDemandResult:
    session = resolve_session(sessions, session_query)
    talker = str(session.get("username") or "")
    if not talker:
        raise SessionSelectionError("候选会话缺少 username，无法导出。")

    active_cfg = _config_for_on_demand(cfg, group_window=group_window)
    root = Path(out_root).expanduser().resolve()
    raw_root = root / "_raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    staging_root = _make_short_staging_root(root)

    api = cfg.export_backend.weflow_api
    contacts = client.fetch_contacts(limit=5000)
    group_members = client.fetch_group_members(talker) if talker.endswith("@chatroom") else []
    messages = client.fetch_messages(
        talker,
        start=start,
        end=end,
        media=api.media_localize,
    )
    transcriber, unavailable_reason = _make_transcriber(
        active_cfg,
        enabled=enable_asr,
        transcriber_factory=transcriber_factory or SenseVoiceTranscriber,
    )
    try:
        staging_session_dir = write_session_export(
            staging_root,
            session,
            messages,
            start=start,
            end=end,
            contacts=contacts,
            group_members=group_members,
            self_wxids=active_cfg.user.self_wxids,
            transcriber=transcriber,
            asr_unavailable_reason=unavailable_reason,
            emit_emotion=active_cfg.asr.emit_emotion,
            require_media=api.media_localize,
            appmsg_text_max_chars=api.appmsg_text_max_chars,
        )
        written = (archive_fn or archive)(
            staging_root,
            config=active_cfg,
            output_root=root,
            image_mode="preserve_paths",
            clear_first=False,
        )
        output_session_dir, diary_files = _restore_range_directory(root, staging_session_dir.name, written)
        raw_session_dir = _publish_raw_session(staging_root, raw_root, staging_session_dir.name)
    finally:
        if transcriber is not None and hasattr(transcriber, "close"):
            transcriber.close()
        if staging_root.exists():
            shutil.rmtree(staging_root)

    if copy_media:
        _copy_media_tree(raw_session_dir / "media", output_session_dir / "media")
    merged_file = _write_merged(output_session_dir, diary_files) if merged else None
    return ExportOnDemandResult(
        session=session,
        raw_session_dir=raw_session_dir,
        output_session_dir=output_session_dir,
        diary_files=diary_files,
        merged_file=merged_file,
    )


def resolve_session(sessions: list[dict[str, Any]], query: str) -> dict[str, Any]:
    needle = query.strip()
    if not needle:
        raise SessionSelectionError("会话查询不能为空。")
    exact = [item for item in sessions if str(item.get("username") or "") == needle]
    if exact:
        return _select_one(exact, needle)

    matches = [item for item in sessions if needle in str(item.get("displayName") or "")]
    if not matches:
        raise SessionSelectionError(f"未找到会话：{needle}")
    return _select_one(matches, needle)


def _select_one(matches: list[dict[str, Any]], query: str) -> dict[str, Any]:
    if len(matches) == 1:
        return matches[0]
    lines = [f"会话「{query}」命中多个候选，请改用 wxid："]
    lines.extend(f"  - {_session_label(item)}" for item in matches)
    raise SessionSelectionError("\n".join(lines), exit_code=2)


def _print_session_candidates(sessions: list[dict[str, Any]], keyword: str) -> None:
    needle = keyword.strip()
    matches = [
        item
        for item in sessions
        if needle in str(item.get("username") or "") or needle in str(item.get("displayName") or "")
    ]
    if not matches:
        print(f"未找到会话：{needle}")
        return
    for item in matches:
        print(f"- {_session_label(item)}")


def _session_label(session: dict[str, Any]) -> str:
    username = str(session.get("username") or "<缺少 wxid>")
    display_name = str(session.get("displayName") or session.get("nickname") or username)
    return f"{display_name} ({username})"


def _config_for_on_demand(cfg: Config, *, group_window: bool) -> Config:
    window = replace(cfg.preprocessing.group_context_window, enabled=group_window)
    preprocessing = replace(cfg.preprocessing, group_context_window=window)
    return replace(cfg, preprocessing=preprocessing)


def _make_client(cfg: Config) -> WeflowApiClient:
    api = cfg.export_backend.weflow_api
    return WeflowApiClient(
        api.base_url,
        api.access_token,
        timeout=api.request_timeout_sec,
        message_timeout=api.message_request_timeout_sec,
    )


def _make_transcriber(
    cfg: Config,
    *,
    enabled: bool,
    transcriber_factory: Callable[..., Any],
) -> tuple[Any, str]:
    if not enabled:
        return None, "ASR已通过 --no-asr 关闭"
    engine = cfg.asr.engine
    if not engine:
        return None, "ASR未启用"
    if engine == "whisper":
        return None, "whisper引擎本期未就绪"
    if engine != "sensevoice":
        return None, f"未知ASR引擎:{engine}"
    worker_python = cfg.asr.worker_python
    if worker_python is None or not worker_python.is_file():
        return None, "SenseVoice worker_python未配置或不可执行"
    transcriber = transcriber_factory(
        worker_python=worker_python,
        worker_script=cfg.asr.worker_script,
        model=cfg.asr.model,
        language=cfg.asr.language,
        device=cfg.asr.device,
        startup_timeout_sec=cfg.asr.worker_startup_timeout_sec,
        request_timeout_sec=cfg.asr.worker_request_timeout_sec,
    )
    return transcriber, "SenseVoice worker未就绪"


def _restore_range_directory(
    root: Path,
    range_name: str,
    written: list[Path],
) -> tuple[Path, list[Path]]:
    source_name = DATE_SUFFIX_RE.sub("", range_name)
    source = root / source_name
    target = root / range_name
    if source != target and source.exists():
        _move_written_files(source, target, written)

    diary_files: list[Path] = []
    for path in written:
        relative = Path(path).relative_to(root)
        candidate = target / relative.relative_to(source_name)
        if candidate.is_file() and candidate not in diary_files:
            diary_files.append(candidate)
    diary_files.sort(key=lambda item: item.name)
    return target, diary_files


def _publish_raw_session(staging_root: Path, raw_root: Path, session_name: str) -> Path:
    """Publish only this run's canonical raw session into the accumulating raw root."""

    source = staging_root / session_name
    destination = raw_root / session_name
    if not source.is_dir():
        raise FileNotFoundError(f"本次 raw 会话目录不存在：{source}")
    if destination.exists():
        # This exact directory is a prior generated snapshot of the same range;
        # other range directories under raw_root remain untouched.
        shutil.rmtree(destination)
    shutil.move(str(source), str(destination))
    return destination


def _make_short_staging_root(root: Path) -> Path:
    """Create a short per-run root; core adds its own staging suffix below it."""

    for _ in range(20):
        candidate = root / f"._od{secrets.token_hex(2)}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("无法创建唯一的按需导出 staging 目录。")


def _move_written_files(source: Path, target: Path, written: list[Path]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    moved_dirs: set[Path] = set()
    for path in written:
        try:
            relative = Path(path).relative_to(source)
        except ValueError:
            continue
        if not path.is_file():
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.unlink()
        shutil.move(str(path), str(destination))
        moved_dirs.add(path.parent)

    moved_dirs.add(source)
    for directory in sorted(moved_dirs, key=lambda item: len(item.parts), reverse=True):
        current = directory
        while current.exists():
            try:
                current.rmdir()
            except OSError:
                break
            if current == source:
                break
            current = current.parent


def _copy_media_tree(source: Path, target: Path) -> None:
    if not source.is_dir():
        return
    for child in source.rglob("*"):
        if not child.is_file():
            continue
        relative = child.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(child, destination)


def _write_merged(session_dir: Path, diary_files: list[Path]) -> Path | None:
    if not diary_files:
        return None
    chunks = [path.read_text(encoding="utf-8").rstrip() for path in diary_files]
    body = "\n\n".join(chunk for chunk in chunks if chunk)
    destination = session_dir.parent / f"{session_dir.name}.md"
    destination.write_text(f"{body}\n" if body else "", encoding="utf-8")
    return destination


def _resolve_config_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def _resolve_output_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def _format_os_error(exc: OSError) -> str:
    path_length = _os_error_path_length(exc)
    if (
        os.name == "nt"
        and path_length >= WINDOWS_PATH_WARNING_LENGTH
        and not _windows_long_paths_enabled()
    ):
        return (
            "按需导出失败：输出路径过长（Windows 单路径上限 260 字符，"
            f"当前 {path_length} 字符）。请改用更短的 --out 目录，"
            "或启用 Windows 长路径支持后重试。"
        )
    return f"按需导出失败：{exc}"


def _os_error_path_length(exc: OSError) -> int:
    candidates = [getattr(exc, "filename", None), getattr(exc, "filename2", None)]
    return max((len(str(value)) for value in candidates if value), default=0)


def _windows_long_paths_enabled() -> bool:
    if os.name != "nt":
        return True
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
            return bool(value)
    except (OSError, ImportError):
        return False


def _parse_day(value: str | None) -> date:
    text = str(value or "").strip()
    if COMPACT_DAY_RE.fullmatch(text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if not DAY_RE.fullmatch(text):
        raise ValueError(f"日期格式无效：{value!r}；应为 yyyy-mm-dd 或 yyyymmdd。")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"日期无效：{value!r}。") from exc


def _require_export_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    missing = [name for name in ("session", "start", "end", "out") if not getattr(args, name)]
    if missing:
        parser.error("导出模式缺少参数：" + ", ".join(f"--{name}" for name in missing))


if __name__ == "__main__":
    raise SystemExit(main())
