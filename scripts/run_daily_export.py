from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_diary_core.archiving import archive, archive_chats_for
from wechat_diary_core.backends import ExporterBackend, create_backend
from wechat_diary_core.backends.weflow.driver import DriverCommandError, DriverUnavailable
from wechat_diary_core.backends.weflow.launcher import stop_weflow_processes
from wechat_diary_core.backup_state import evaluate_backup_state
from wechat_diary_core.config import Config, load_config
from wechat_diary_core.preprocessing import archive_moments_for
from wechat_diary_core.preprocessing import collect_voice_transcription_failures
from wechat_diary_core.workspace import rotate_export_workspace
from scripts.process_existing_raw import archive_existing_processed


SECTION_RE = re.compile(r"(?m)^[ \t]*\[([^\]]+)\][ \t]*$")


@dataclass(frozen=True)
class DailyExportResult:
    day: str
    rotation_target: Path | None
    diary_files: list[Path]
    self_moment_files: list[Path]
    sidecar_chat_files: list[Path]
    sidecar_moment_files: list[Path]
    # Non-critical (sidecar moments) stages that failed but did not abort the
    # chat diary. Empty = clean run.
    partial_failures: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RawTreeSnapshot:
    file_count: int
    dir_count: int
    latest_mtime_ns: int
    total_size: int


@dataclass
class DailyExportDeps:
    backend: ExporterBackend | None = None
    backend_factory: Callable[[str, Config], ExporterBackend] = create_backend
    wait_for_raw_exports_stable: Callable[..., Any] = None  # type: ignore[assignment]
    rotate_export_workspace: Callable[..., Any] = rotate_export_workspace
    run_voice_fallback_script: Callable[..., Any] = None  # type: ignore[assignment]
    archive_existing_processed: Callable[..., Any] = archive_existing_processed
    archive: Callable[..., Any] = archive
    archive_chats_for: Callable[..., Any] = archive_chats_for
    archive_moments_for: Callable[..., Any] = archive_moments_for

    def __post_init__(self) -> None:
        if self.wait_for_raw_exports_stable is None:
            self.wait_for_raw_exports_stable = wait_for_raw_exports_stable
        if self.run_voice_fallback_script is None:
            self.run_voice_fallback_script = run_voice_fallback_script


class DailyExportStageError(RuntimeError):
    def __init__(self, stage: str, cause: BaseException) -> None:
        self.stage = stage
        self.cause = cause
        super().__init__(f"{stage}: {cause}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Run the local WeFlow daily export pipeline through processed markdown.")
    parser.add_argument("--config", default="config.toml", help="Path to the local config file.")
    parser.add_argument(
        "--no-config-prompt",
        action="store_true",
        help="Do not prompt to create or fill missing local config values.",
    )
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (Path.cwd() / config_path).resolve()

    cfg: Config | None = None
    export_started = False
    try:
        ensure_local_config(
            config_path=config_path,
            example_path=ROOT / "config.example.toml",
            prompt=not args.no_config_prompt,
        )
        cfg = load_config(config_path)
        export_started = True
        result = run_daily_export(cfg)
    except DailyExportStageError as exc:
        print(f"\nFAILED at stage: {exc.stage}", file=sys.stderr)
        print(f"Reason: {exc.cause}", file=sys.stderr)
        detail = _stage_error_detail(exc.cause)
        if detail:
            print(f"DETAIL: {detail}", file=sys.stderr)
        if isinstance(exc.cause, DriverUnavailable):
            print(
                "Next step: close WeFlow completely, then run Start-DailyExport.bat again. "
                "The script will relaunch WeFlow with the CDP flag.",
                file=sys.stderr,
            )
        _cleanup_weflow_after_failure(cfg)
        return 1
    except Exception as exc:
        print(f"\nFAILED before export completed: {exc}", file=sys.stderr)
        if export_started:
            _cleanup_weflow_after_failure(cfg)
        return 1

    completed_with_warnings = bool(result.partial_failures)
    print("\nDaily export completed with warnings." if completed_with_warnings else "\nDaily export completed.")
    print(f"Day: {result.day}")
    print(f"Archive root: {result.rotation_target or 'none'}")
    print(f"Diary processed files: {len(result.diary_files)}")
    self_moments_note = "" if cfg.daily_export.self_moments_configured else "（未配置，本轮已跳过——见上方 [WARN]）"
    print(f"Self moments files: {len(result.self_moment_files)}{self_moments_note}")
    print(f"Sidecar chat files: {len(result.sidecar_chat_files)}")
    print(f"Sidecar moments files: {len(result.sidecar_moment_files)}")
    for path in result.diary_files + result.self_moment_files + result.sidecar_chat_files + result.sidecar_moment_files:
        print(f"- {path}")
    _warn_if_backup_stale(cfg)

    if completed_with_warnings:
        print(
            "[WARN] 本轮以下可选阶段失败、已跳过: "
            + ", ".join(result.partial_failures)
            + "。聊天 diary 已正常产出；这些朋友圈可在修复/WeFlow 空闲后单独补跑。",
            file=sys.stderr,
        )
        return 1
    return 0


def _warn_if_backup_stale(cfg: Config) -> None:
    """Surface a dead bundle backup where a human actually looks every day.

    The daily export is the only thing that runs every day, so it is the only
    channel that would have caught the previous month-long silent failure.
    Never blocks and never changes the exit code -- backup health is not a
    reason to fail an otherwise good export.
    """
    try:
        state = evaluate_backup_state(cfg.backup, skill_root=ROOT)
    except Exception:  # noqa: BLE001 - a broken check must not break the export
        return
    if state.needs_attention:
        print(f"[WARN] {state.message}", file=sys.stderr)


def ensure_local_config(
    config_path: Path,
    example_path: Path,
    *,
    prompt: bool = True,
    input_func: Callable[[str], str] = input,
    password_func: Callable[[str], str] | None = None,
) -> None:
    if not config_path.exists():
        if not example_path.exists():
            raise FileNotFoundError(f"Missing config file and example template: {example_path}")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(example_path, config_path)
        print(f"Created local config: {config_path}")

    text = config_path.read_text(encoding="utf-8")
    data = _loads_toml(text, config_path)
    backend_name = _selected_backend(data)

    if backend_name == "weflow" and _needs_weflow_path(data):
        if not prompt:
            raise RuntimeError("config.toml is missing a usable [export_backend.weflow].weflow_exe value.")
        # input()'s prompt arg goes to stdout, which under .bat → cmd /c 2>&1 |
        # PowerShell ForEach-Object is block-buffered; a partial line stays
        # invisible until something flushes. Print + flush explicitly so the
        # double-click user actually sees the prompt before stdin blocks.
        print("WeFlow.exe path: ", end="", flush=True)
        weflow_exe = input_func("").strip().strip('"')
        if not weflow_exe:
            raise RuntimeError("WeFlow.exe path is required.")
        text = _set_toml_value(text, _weflow_config_section(data), "weflow_exe", _toml_string(weflow_exe))
        data = _loads_toml(text, config_path)

    if backend_name == "weflow_api":
        api = (data.get("export_backend") or {}).get("weflow_api") or {}
        if not str(api.get("base_url") or "").strip():
            text = _set_toml_value(
                text,
                "export_backend.weflow_api",
                "base_url",
                _toml_string("http://127.0.0.1:5031"),
            )
            data = _loads_toml(text, config_path)
            api = (data.get("export_backend") or {}).get("weflow_api") or {}
        if not str(api.get("access_token") or "").strip():
            if not prompt:
                raise RuntimeError(
                    "config.toml 缺少 [export_backend.weflow_api].access_token；"
                    "请在 WeFlow → 设置 → API 服务中生成固定 token。"
                )
            # Production uses getpass so the token never echoes. Tests and
            # embedders that inject an input function can inject the secret via
            # the same controlled channel without touching the real terminal.
            secret_reader = password_func or (getpass.getpass if input_func is input else input_func)
            token = secret_reader("WeFlow API Access Token（输入不回显）: ").strip()
            if not token:
                raise RuntimeError("WeFlow API Access Token 不能为空。")
            text = _set_toml_value(
                text,
                "export_backend.weflow_api",
                "access_token",
                _toml_string(token),
            )
            data = _loads_toml(text, config_path)

    daily_export_section = data.get("daily_export") or {}

    # Only add missing keys. Rewriting present keys would clobber any inline
    # `# comment` the user inherited from config.example.toml.
    if "target_usernames" not in daily_export_section:
        text = _set_toml_value(text, "daily_export", "target_usernames", _toml_array([]))
    if "skip_official_accounts" not in daily_export_section:
        text = _set_toml_value(text, "daily_export", "skip_official_accounts", "true")
    if "self_moments_usernames" not in daily_export_section and prompt:
        # Never silently write [] here: an auto-written empty list is
        # indistinguishable from a deliberate opt-out and made the runner skip
        # the user's own moments for whole runs. Ask once instead; an empty
        # answer becomes an explicit [] so we do not nag on every run.
        print(
            "未配置「自己的朋友圈」导出（diary 素材）。输入你自己的 wxid（多个用逗号分隔）；"
            "直接回车 = 不导出（之后可在 config.toml [daily_export].self_moments_usernames 修改）。",
            flush=True,
        )
        print("self moments wxid: ", end="", flush=True)
        self_moments_values = _split_values(input_func(""))
        text = _set_toml_value(text, "daily_export", "self_moments_usernames", _toml_array(self_moments_values))
    if "target_processed_subroot" not in daily_export_section:
        text = _set_toml_value(text, "daily_export", "target_processed_subroot", _toml_string("_targets"))
    if "voice_fallback_script" not in daily_export_section:
        text = _set_toml_value(text, "daily_export", "voice_fallback_script", _toml_string(""))
    if "cleanup_mode" not in daily_export_section:
        text = _set_toml_value(text, "daily_export", "cleanup_mode", _toml_string("archive"))
    if "restart_weflow" not in daily_export_section:
        text = _set_toml_value(text, "daily_export", "restart_weflow", "true")

    target_usernames = _string_list(daily_export_section.get("target_usernames"))
    data = _loads_toml(text, config_path)
    voice_users = _string_list((data.get("user") or {}).get("voice_transcribe_usernames"))
    if not voice_users and target_usernames:
        # Real value change; any inline comment on the empty default is lost.
        text = _set_toml_value(text, "user", "voice_transcribe_usernames", _toml_array(target_usernames))

    config_path.write_text(text, encoding="utf-8")


def run_daily_export(
    cfg: Config,
    *,
    deps: DailyExportDeps | None = None,
    day: date | None = None,
) -> DailyExportResult:
    active_deps = deps or DailyExportDeps()
    backend = active_deps.backend or active_deps.backend_factory(cfg.export_backend.backend, cfg)
    export_day = day or (datetime.now().date() - timedelta(days=1))
    day_iso = export_day.isoformat()
    target_usernames = list(cfg.daily_export.target_usernames)
    self_moments_usernames = list(cfg.daily_export.self_moments_usernames)

    print(f"Daily export day: {day_iso}")
    print(f"Raw root: {cfg.paths.raw}")
    print(f"Processed root: {cfg.paths.processed}")
    if target_usernames:
        print(f"Target sidecar contacts: {len(target_usernames)}")
    else:
        print("Target sidecar contacts: none; moments and sidecar archives will be skipped.")
    if self_moments_usernames:
        print(f"Self moments contacts: {len(self_moments_usernames)}")
    elif cfg.daily_export.self_moments_configured:
        print("Self moments contacts: none (explicitly disabled); diary self moments will be skipped.")
    else:
        print("Self moments contacts: NOT CONFIGURED; diary self moments will be skipped this run.")
        print(
            "[WARN] 自己的朋友圈未配置导出：在 config.toml [daily_export].self_moments_usernames "
            "填入自己的 wxid；确认不需要则设为 [] 显式关闭。"
        )

    prepared = False
    moments_failures: list[str] = []
    target_moments_ok = False
    self_moments_ok = False
    if backend.name == "manual":
        print(
            "manual backend: using existing live raw; "
            "prepare/rotate/export stages skipped."
        )
        rotation = None
        _run_stage("validate_raw_root", lambda: _validate_manual_raw_root(cfg.paths.raw))
        _run_stage(
            "archive_existing_processed",
            lambda: active_deps.archive_existing_processed(
                cfg.paths.processed,
                cfg.paths.archived / "processed",
            ),
        )
        # Existing raw may already contain either moments stream. Treat it like
        # process_existing_raw: configured consumers inspect what is present.
        target_moments_ok = bool(target_usernames)
        self_moments_ok = bool(self_moments_usernames)
    else:
        try:
            # Prepare is deliberately before rotation: a backend that cannot
            # become ready must not mutate the live raw/processed roots.
            _run_stage("prepare_backend", backend.prepare)
            prepared = True
            rotation = _run_stage(
                "rotate_workspace",
                lambda: active_deps.rotate_export_workspace(
                    cfg,
                    label="daily_export",
                    mode=cfg.daily_export.cleanup_mode,
                ),
            )

            voice_usernames = list(cfg.user.voice_transcribe_usernames) or target_usernames
            if voice_usernames and "voice_transcribe" in backend.capabilities:
                _run_stage("voice_transcribe", lambda: backend.transcribe_voices(voice_usernames))
            elif voice_usernames:
                if backend.name == "weflow_api":
                    print("voice_transcribe skipped: backend 'weflow_api' transcribes inline during export_all_chats.")
                else:
                    print(
                        f"voice_transcribe skipped: backend '{backend.name}' does not support "
                        "a separate voice_transcribe stage."
                    )
            else:
                print("voice_transcribe skipped: no configured contacts.")

            _run_stage("export_all_chats", lambda: backend.export_chats(export_day))
            _review_session_failures(backend)
            for failure in getattr(backend, "partial_failures", []):
                if failure not in moments_failures:
                    moments_failures.append(failure)
            _run_stage(
                "wait_raw_exports_stable",
                lambda: _settle_raw_exports(backend, cfg.paths.raw, active_deps),
            )

            # Moments are supplementary: isolate each failure so the primary
            # chat diary and any successful stream can still complete.
            if target_usernames and "moments" in backend.capabilities:
                target_moments_ok = _run_moments_stage(
                    "export_target_moments",
                    lambda: backend.export_moments(target_usernames, export_day),
                    "wait_raw_exports_stable_after_moments",
                    lambda: _settle_raw_exports(backend, cfg.paths.raw, active_deps),
                    failures=moments_failures,
                )
            elif target_usernames:
                print(f"export_target_moments skipped: backend '{backend.name}' does not support moments.")
            else:
                print("export_target_moments skipped: no target sidecar contacts.")

            if self_moments_usernames and "moments" in backend.capabilities:
                self_moments_ok = _run_moments_stage(
                    "export_self_moments",
                    lambda: backend.export_moments(self_moments_usernames, export_day),
                    "wait_raw_exports_stable_after_self_moments",
                    lambda: _settle_raw_exports(backend, cfg.paths.raw, active_deps),
                    failures=moments_failures,
                )
            elif self_moments_usernames:
                print(f"export_self_moments skipped: backend '{backend.name}' does not support moments.")
            else:
                print("export_self_moments skipped: no configured self moments contacts.")
            # Backends may degrade individual media items without failing the
            # moments stage. Surface those warnings through the same tri-state
            # result used for isolated session and sidecar failures.
            for failure in getattr(backend, "partial_failures", []):
                if failure not in moments_failures:
                    moments_failures.append(failure)
        finally:
            if prepared:
                _finish_backend(backend, moments_failures)

    if cfg.daily_export.voice_fallback_script:
        _run_stage(
            "voice_fallback",
            lambda: active_deps.run_voice_fallback_script(cfg.daily_export.voice_fallback_script, cfg),
        )
    else:
        print("voice_fallback skipped: no configured script.")

    with collect_voice_transcription_failures():
        diary_files = _run_stage(
            "archive_diary_processed",
            lambda: active_deps.archive(cfg.paths.raw, config=cfg, clear_first=True),
        )
        self_moment_files = []
        if self_moments_usernames and self_moments_ok:
            self_moment_files = _run_stage(
                "archive_self_moments",
                lambda: active_deps.archive_moments_for(
                    self_moments_usernames,
                    config=cfg,
                    subroot="朋友圈_自己",
                    clear_first=True,
                ),
            )
        elif self_moments_usernames:
            print("archive_self_moments skipped: export_self_moments did not complete.")

        subroot = _normalize_subroot(cfg.daily_export.target_processed_subroot)
        sidecar_chat_files = []
        sidecar_moment_files = []
        if target_usernames:
            # target chats come from export_all_chats (not the moments export),
            # so they archive regardless of whether target moments succeeded.
            sidecar_chat_files = _run_stage(
                "archive_target_chats",
                lambda: active_deps.archive_chats_for(
                    target_usernames,
                    config=cfg,
                    subroot=f"{subroot}/chats",
                    image_mode="preserve_paths",
                    clear_first=True,
                ),
            )
            if target_moments_ok:
                sidecar_moment_files = _run_stage(
                    "archive_target_moments",
                    lambda: active_deps.archive_moments_for(
                        target_usernames,
                        config=cfg,
                        subroot=f"{subroot}/moments",
                        clear_first=True,
                    ),
                )
            else:
                print("archive_target_moments skipped: export_target_moments did not complete.")

    return DailyExportResult(
        day=day_iso,
        rotation_target=getattr(rotation, "target", None),
        diary_files=list(diary_files),
        self_moment_files=list(self_moment_files),
        partial_failures=list(moments_failures),
        sidecar_chat_files=list(sidecar_chat_files),
        sidecar_moment_files=list(sidecar_moment_files),
    )


def wait_for_raw_exports_stable(
    root: str | Path,
    *,
    quiet_seconds: float = 8.0,
    timeout: float = 180.0,
    poll_interval: float = 1.0,
    min_files: int = 1,
) -> RawTreeSnapshot:
    """Wait until WeFlow has stopped mutating the raw export tree.

    WeFlow can mark a task completed in the UI before its JSON/media files have
    all landed on disk. The processed archive step reads the filesystem, so it
    needs a short quiet window after each GUI export task.
    """
    raw_root = Path(root)
    deadline = time.monotonic() + timeout
    last_snapshot: RawTreeSnapshot | None = None
    stable_since: float | None = None
    latest_snapshot = RawTreeSnapshot(file_count=0, dir_count=0, latest_mtime_ns=0, total_size=0)

    while True:
        now = time.monotonic()
        snapshot = _snapshot_raw_tree(raw_root)
        latest_snapshot = snapshot
        has_enough_files = snapshot.file_count >= min_files

        if snapshot != last_snapshot:
            last_snapshot = snapshot
            stable_since = now if has_enough_files else None
        elif has_enough_files:
            if stable_since is None:
                stable_since = now
            if now - stable_since >= quiet_seconds:
                return snapshot

        if now >= deadline:
            raise TimeoutError(
                "Raw export directory did not become stable "
                f"within {timeout:.0f}s "
                f"(files={latest_snapshot.file_count}, dirs={latest_snapshot.dir_count}, "
                f"min_files={min_files})."
            )
        time.sleep(poll_interval)


def run_voice_fallback_script(script_path: str | Path, cfg: Config) -> None:
    script = Path(script_path)
    if not script.exists():
        raise FileNotFoundError(f"Voice fallback script does not exist: {script}")

    target_runs = list(cfg.daily_export.target_usernames) or [""]
    for target in target_runs:
        command = [sys.executable, str(script), "--raw-root", str(cfg.paths.raw)]
        if target:
            command.extend(["--target-wxid", target])
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        subprocess.run(command, cwd=cfg.base_dir, env=env, check=True)


def _snapshot_raw_tree(root: Path) -> RawTreeSnapshot:
    if not root.exists():
        return RawTreeSnapshot(file_count=0, dir_count=0, latest_mtime_ns=0, total_size=0)

    file_count = 0
    dir_count = 0
    latest_mtime_ns = 0
    total_size = 0
    for dir_path, dir_names, file_names in os.walk(root):
        dir_count += len(dir_names)
        try:
            latest_mtime_ns = max(latest_mtime_ns, Path(dir_path).stat().st_mtime_ns)
        except OSError:
            latest_mtime_ns = max(latest_mtime_ns, time.time_ns())
        for file_name in file_names:
            path = Path(dir_path) / file_name
            try:
                stat = path.stat()
            except OSError:
                latest_mtime_ns = max(latest_mtime_ns, time.time_ns())
                continue
            file_count += 1
            total_size += stat.st_size
            latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
    return RawTreeSnapshot(
        file_count=file_count,
        dir_count=dir_count,
        latest_mtime_ns=latest_mtime_ns,
        total_size=total_size,
    )


def _run_stage(stage: str, action: Callable[[], Any]) -> Any:
    print(f"[{datetime.now():%H:%M:%S}] {stage}...")
    try:
        result = action()
    except Exception as exc:
        raise DailyExportStageError(stage, exc) from exc
    print(f"[{datetime.now():%H:%M:%S}] {stage} done.")
    return result


def _stage_error_detail(cause: BaseException) -> str:
    if isinstance(cause, DriverCommandError):
        return cause.detail
    return ""


def _cleanup_weflow_after_failure(cfg: Config | None) -> None:
    if cfg is not None and cfg.export_backend.backend != "weflow":
        return
    timeout = cfg.automation.launch_timeout_sec if cfg is not None else 30
    try:
        stopped = stop_weflow_processes(timeout=timeout)
    except Exception as exc:
        print(f"[WARN] Failed to stop WeFlow after export failure: {exc}", file=sys.stderr)
        return
    if stopped is False:
        print("[WARN] Timed out stopping WeFlow after export failure; close WeFlow manually before retrying.", file=sys.stderr)


def _try_backend_stage(
    stage: str,
    action: Callable[[], Any],
    *,
    failures: list[str],
) -> bool:
    """Run a non-critical backend stage; record a failure and continue.

    Moments exports are sidecar/supplementary — a failure here (e.g. WeFlow busy
    and CDP slow) must not abort the chat diary, the primary output. Returns True
    only on success so the caller can gate its dependent archive step.
    """
    try:
        _run_stage(stage, action)
        return True
    except DailyExportStageError as exc:
        failures.append(exc.stage)
        print(
            f"[WARN] 阶段 {exc.stage} 失败，已跳过（聊天 diary 不受影响）。原因: {exc.cause}",
            file=sys.stderr,
        )
        detail = _stage_error_detail(exc.cause)
        if detail:
            print(f"[WARN] DETAIL: {detail}", file=sys.stderr)
        return False


def _run_moments_stage(
    export_stage: str,
    export_action: Callable[[], Any],
    settle_stage: str,
    settle_action: Callable[[], Any],
    *,
    failures: list[str],
) -> bool:
    """Export a sidecar moments set with failure isolation.

    Returns True only if the export itself succeeded (caller gates the archive
    step on this). Once the export succeeds, the post-export settle wait is
    best-effort: a slow settle must not throw away an export that completed.
    """
    if not _try_backend_stage(export_stage, export_action, failures=failures):
        return False
    try:
        _run_stage(settle_stage, settle_action)
    except DailyExportStageError as exc:
        print(
            f"[WARN] {exc.stage} 未在限时内稳定（{export_stage} 已完成，继续处理）。原因: {exc.cause}",
            file=sys.stderr,
        )
    return True


def _validate_manual_raw_root(raw_root: Path) -> None:
    if not raw_root.exists():
        raise FileNotFoundError(
            f"Raw root does not exist: {raw_root}。请将 canonical raw 放进该路径后重试。"
        )
    if not raw_root.is_dir():
        raise NotADirectoryError(
            f"Raw root is not a directory: {raw_root}。请将 canonical raw 放进该路径后重试。"
        )
    if not any(raw_root.iterdir()):
        raise FileNotFoundError(
            f"Raw root is empty: {raw_root}。请将 canonical raw 放进该路径后重试。"
        )


def _finish_backend(backend: ExporterBackend, partial_failures: list[str]) -> None:
    """Shut down backend-owned resources without masking the export result."""

    try:
        _run_stage("shutdown_backend", backend.shutdown)
    except DailyExportStageError as exc:
        partial_failures.append("shutdown_backend")
        print(
            f"[WARN] backend shutdown failed; export pipeline will continue: {exc.cause}",
            file=sys.stderr,
        )


def _review_session_failures(backend: ExporterBackend) -> None:
    review = getattr(backend, "review_session_failures", None)
    if not callable(review):
        return
    isatty = getattr(sys.stdin, "isatty", None)
    interactive = bool(isatty()) if callable(isatty) else False
    review(interactive=interactive, input_func=input)


def _loads_toml(text: str, path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"Invalid TOML in {path}: {exc}") from exc


def _needs_weflow_path(data: dict[str, Any]) -> bool:
    backend = _selected_backend(data)
    if backend != "weflow":
        return False
    export_backend = data.get("export_backend") or {}
    weflow = {
        **(data.get("automation") or {}),
        **(export_backend.get("weflow") or {}),
    }
    value = str(weflow.get("weflow_exe") or "").strip()
    return not value or value == "C:/Path/To/WeFlow.exe"


def _selected_backend(data: dict[str, Any]) -> str:
    export_backend = data.get("export_backend") or {}
    explicit = str(export_backend.get("backend") or "").strip().lower()
    if explicit:
        return explicit
    if isinstance(data.get("automation"), dict):
        return "weflow"
    return "weflow_api"


def _settle_raw_exports(backend: ExporterBackend, raw_root: Path, deps: DailyExportDeps) -> None:
    if backend.name == "weflow_api":
        print("raw settle skipped: WeFlow API publishes validated session directories synchronously.")
        return
    deps.wait_for_raw_exports_stable(raw_root, min_files=1)


def _weflow_config_section(data: dict[str, Any]) -> str:
    export_backend = data.get("export_backend") or {}
    return "export_backend.weflow" if "weflow" in export_backend else "automation"


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _split_values(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,，]", value) if part.strip()]


def _normalize_subroot(value: str) -> str:
    cleaned = value.strip().strip("/\\")
    return cleaned or "_targets"


def _toml_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _set_toml_value(text: str, section: str, key: str, value: str) -> str:
    header, body_start, body_end = _find_section(text, section)
    line = f"{key} = {value}"
    if header is None:
        separator = "\n\n" if text and not text.endswith("\n\n") else ""
        return f"{text}{separator}[{section}]\n{line}\n"

    body = text[body_start:body_end]
    key_re = re.compile(rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=.*$")
    match = key_re.search(body)
    if match:
        body = body[: match.start()] + line + body[match.end() :]
    else:
        tail = body if body.startswith("\n") else f"\n{body}" if body else "\n"
        body = f"\n{line}{tail}"
    return text[:body_start] + body + text[body_end:]


def _find_section(text: str, section: str) -> tuple[re.Match[str] | None, int, int]:
    matches = list(SECTION_RE.finditer(text))
    for index, match in enumerate(matches):
        if match.group(1).strip() == section:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            return match, match.end(), end
    return None, len(text), len(text)


if __name__ == "__main__":
    raise SystemExit(main())
