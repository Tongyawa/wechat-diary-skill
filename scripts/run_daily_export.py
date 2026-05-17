from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_diary_core.archiving import archive, archive_chats_for
from wechat_diary_core.config import Config, load_config
from wechat_diary_core.preprocessing import archive_moments_for
from wechat_diary_core.weflow_automation.cdp_driver import CdpDriver
from wechat_diary_core.weflow_automation.driver import DriverUnavailable, ElementNotFound
from wechat_diary_core.weflow_automation.exporter import export_all_chats, export_moments_for
from wechat_diary_core.weflow_automation.launcher import ensure_weflow_running, stop_weflow_processes
from wechat_diary_core.weflow_automation.voice_transcribe import batch_transcribe_voices_for
from wechat_diary_core.workspace import rotate_export_workspace


SECTION_RE = re.compile(r"(?m)^\s*\[([^\]]+)\]\s*$")


@dataclass(frozen=True)
class DailyExportResult:
    day: str
    rotation_target: Path | None
    diary_files: list[Path]
    sidecar_chat_files: list[Path]
    sidecar_moment_files: list[Path]


@dataclass
class DailyExportDeps:
    stop_weflow_processes: Callable[..., Any] = stop_weflow_processes
    ensure_weflow_running: Callable[..., Any] = ensure_weflow_running
    wait_for_ready_page: Callable[..., Any] = None  # type: ignore[assignment]
    rotate_export_workspace: Callable[..., Any] = rotate_export_workspace
    batch_transcribe_voices_for: Callable[..., Any] = batch_transcribe_voices_for
    export_all_chats: Callable[..., Any] = export_all_chats
    export_moments_for: Callable[..., Any] = export_moments_for
    archive: Callable[..., Any] = archive
    archive_chats_for: Callable[..., Any] = archive_chats_for
    archive_moments_for: Callable[..., Any] = archive_moments_for

    def __post_init__(self) -> None:
        if self.wait_for_ready_page is None:
            self.wait_for_ready_page = wait_for_ready_page


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
        config_path = ROOT / config_path

    try:
        ensure_local_config(
            config_path=config_path,
            example_path=ROOT / "config.example.toml",
            prompt=not args.no_config_prompt,
        )
        cfg = load_config(config_path)
        result = run_daily_export(cfg)
    except DailyExportStageError as exc:
        print(f"\nFAILED at stage: {exc.stage}", file=sys.stderr)
        print(f"Reason: {exc.cause}", file=sys.stderr)
        if isinstance(exc.cause, DriverUnavailable):
            print(
                "Next step: close WeFlow completely, then run Start-DailyExport.bat again. "
                "The script will relaunch WeFlow with the CDP flag.",
                file=sys.stderr,
            )
        return 1
    except Exception as exc:
        print(f"\nFAILED before export completed: {exc}", file=sys.stderr)
        return 1

    print("\nDaily export completed.")
    print(f"Day: {result.day}")
    print(f"Rotation archive: {result.rotation_target or 'none'}")
    print(f"Diary processed files: {len(result.diary_files)}")
    print(f"Sidecar chat files: {len(result.sidecar_chat_files)}")
    print(f"Sidecar moments files: {len(result.sidecar_moment_files)}")
    for path in result.diary_files + result.sidecar_chat_files + result.sidecar_moment_files:
        print(f"- {path}")
    return 0


def ensure_local_config(
    config_path: Path,
    example_path: Path,
    *,
    prompt: bool = True,
    input_func: Callable[[str], str] = input,
) -> None:
    if not config_path.exists():
        if not example_path.exists():
            raise FileNotFoundError(f"Missing config file and example template: {example_path}")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(example_path, config_path)
        print(f"Created local config: {config_path}")

    text = config_path.read_text(encoding="utf-8")
    data = _loads_toml(text, config_path)

    if _needs_weflow_path(data):
        if not prompt:
            raise RuntimeError("config.toml is missing a usable [automation].weflow_exe value.")
        weflow_exe = input_func("WeFlow.exe path: ").strip().strip('"')
        if not weflow_exe:
            raise RuntimeError("WeFlow.exe path is required.")
        text = _set_toml_value(text, "automation", "weflow_exe", _toml_string(weflow_exe))
        data = _loads_toml(text, config_path)

    target_usernames = _string_list((data.get("daily_export") or {}).get("target_usernames"))
    if not target_usernames:
        if not prompt:
            raise RuntimeError("config.toml is missing [daily_export].target_usernames.")
        raw_targets = input_func("Target contact wxid or display name (comma-separated): ").strip()
        target_usernames = _split_values(raw_targets)
        if not target_usernames:
            raise RuntimeError("At least one target contact is required.")
        text = _set_toml_value(text, "daily_export", "target_usernames", _toml_array(target_usernames))

    text = _set_toml_value(
        text,
        "daily_export",
        "target_processed_subroot",
        _toml_string((data.get("daily_export") or {}).get("target_processed_subroot") or "_targets"),
    )
    text = _set_toml_value(
        text,
        "daily_export",
        "cleanup_mode",
        _toml_string((data.get("daily_export") or {}).get("cleanup_mode") or "archive"),
    )
    text = _set_toml_value(
        text,
        "daily_export",
        "restart_weflow",
        "true" if (data.get("daily_export") or {}).get("restart_weflow", True) else "false",
    )

    data = _loads_toml(text, config_path)
    voice_users = _string_list((data.get("user") or {}).get("voice_transcribe_usernames"))
    if not voice_users:
        text = _set_toml_value(text, "user", "voice_transcribe_usernames", _toml_array(target_usernames))

    config_path.write_text(text, encoding="utf-8")


def run_daily_export(
    cfg: Config,
    *,
    deps: DailyExportDeps | None = None,
    day: date | None = None,
) -> DailyExportResult:
    active_deps = deps or DailyExportDeps()
    export_day = day or (datetime.now().date() - timedelta(days=1))
    day_iso = export_day.isoformat()
    target_usernames = list(cfg.daily_export.target_usernames)
    if not target_usernames:
        raise RuntimeError("[daily_export].target_usernames must contain at least one contact.")

    print(f"Daily export day: {day_iso}")
    print(f"Raw root: {cfg.paths.raw}")
    print(f"Processed root: {cfg.paths.processed}")

    if cfg.daily_export.restart_weflow:
        _run_stage(
            "stop_weflow",
            lambda: active_deps.stop_weflow_processes(timeout=cfg.automation.launch_timeout_sec),
        )

    rotation = _run_stage(
        "rotate_workspace",
        lambda: active_deps.rotate_export_workspace(
            cfg,
            label="daily_export",
            mode=cfg.daily_export.cleanup_mode,
        ),
    )

    session = _run_stage("start_weflow", lambda: active_deps.ensure_weflow_running(cfg))
    endpoint = getattr(session, "cdp_endpoint", None)
    if endpoint:
        _run_stage("wait_weflow_ready", lambda: active_deps.wait_for_ready_page(endpoint))

    voice_usernames = list(cfg.user.voice_transcribe_usernames) or target_usernames
    if voice_usernames:
        _run_stage(
            "voice_transcribe",
            lambda: active_deps.batch_transcribe_voices_for(voice_usernames, config=cfg),
        )

    _run_stage(
        "export_all_chats",
        lambda: active_deps.export_all_chats(date=export_day, config=cfg, cleanup="skip"),
    )
    _run_stage(
        "export_target_moments",
        lambda: active_deps.export_moments_for(target_usernames, date=export_day, config=cfg),
    )

    diary_files = _run_stage(
        "archive_diary_processed",
        lambda: active_deps.archive(cfg.paths.raw, config=cfg, clear_first=True),
    )

    subroot = _normalize_subroot(cfg.daily_export.target_processed_subroot)
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
    sidecar_moment_files = _run_stage(
        "archive_target_moments",
        lambda: active_deps.archive_moments_for(
            None,
            config=cfg,
            subroot=f"{subroot}/moments",
            clear_first=True,
        ),
    )

    return DailyExportResult(
        day=day_iso,
        rotation_target=getattr(rotation, "target", None),
        diary_files=list(diary_files),
        sidecar_chat_files=list(sidecar_chat_files),
        sidecar_moment_files=list(sidecar_moment_files),
    )


def wait_for_ready_page(endpoint: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        driver: CdpDriver | None = None
        try:
            driver = CdpDriver.connect(endpoint)
            driver.wait_for("朋友圈", timeout=3)
            return
        except (DriverUnavailable, ElementNotFound, OSError) as exc:
            last_error = exc
            time.sleep(1)
        finally:
            if driver is not None:
                driver.close()
    raise DriverUnavailable(f"WeFlow page did not become ready after launch: {last_error}")


def _run_stage(stage: str, action: Callable[[], Any]) -> Any:
    print(f"[{datetime.now():%H:%M:%S}] {stage}...")
    try:
        result = action()
    except Exception as exc:
        raise DailyExportStageError(stage, exc) from exc
    print(f"[{datetime.now():%H:%M:%S}] {stage} done.")
    return result


def _loads_toml(text: str, path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"Invalid TOML in {path}: {exc}") from exc


def _needs_weflow_path(data: dict[str, Any]) -> bool:
    value = str((data.get("automation") or {}).get("weflow_exe") or "").strip()
    return not value or value == "C:/Path/To/WeFlow.exe"


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
    key_re = re.compile(rf"(?m)^\s*{re.escape(key)}\s*=.*$")
    match = key_re.search(body)
    if match:
        body = body[: match.start()] + line + body[match.end() :]
    else:
        prefix = "\n" if body and not body.startswith("\n") else ""
        body = f"{prefix}{line}{body}"
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
