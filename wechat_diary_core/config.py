from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import copy
import tomllib


DEFAULT_CONFIG: dict[str, Any] = {
    "user": {
        "self_wxids": ["filehelper"],
        "voice_transcribe_usernames": [],
    },
    "paths": {
        "raw": "WeFlow-raw-exports",
        "processed": "WeFlow-processed-exports",
        "archived": "WeFlow-archived-exports",
        "insights": "WeFlow-insights",
        "rotation_root": "其他/test/test_archive",
    },
    "automation": {
        "driver": "cdp",
        "weflow_exe": "C:/Path/To/WeFlow.exe",
        "launch_timeout_sec": 90,
        "poll_export_interval_sec": 60,
        "window_geometry": {"width": 1280, "height": 900},
        "electron_accessibility_flag": "--force-renderer-accessibility",
        "electron_cdp_port": 9222,
        "template_fallback": {
            "zoom_reset_shortcut": "ctrl+0",
            "multi_scale": [0.85, 0.9, 0.95, 1.0, 1.05],
            "retry": 3,
        },
    },
    "preprocessing": {
        "skip_emoji_dir": True,
        "voice_fail_log_only": True,
        "time_compress_interval_sec": 120,
        "image_ocr_enabled": True,
        "image_ocr_min_confidence": 0.55,
        "image_ocr_max_inline_chars": 80,
        "group_context_window": {
            "messages_before": 3,
            "messages_after": 5,
            "time_window_minutes": 15,
            "anchor_keywords": [],
        },
    },
    "agent": {
        "cli": "claude",
        "model": "claude-opus-4.6",
        "extra_args": [],
    },
    "skills": {
        "daily": ["wechat-diary"],
    },
    "daily_export": {
        "target_usernames": [],
        "target_processed_subroot": "_targets",
        "cleanup_mode": "archive",
        "restart_weflow": True,
    },
}


@dataclass(frozen=True)
class UserConfig:
    self_wxids: list[str]
    voice_transcribe_usernames: list[str]


@dataclass(frozen=True)
class PathsConfig:
    raw: Path
    processed: Path
    archived: Path
    insights: Path
    rotation_root: Path


@dataclass(frozen=True)
class WindowGeometry:
    width: int
    height: int
    x: int | None = None
    y: int | None = None


@dataclass(frozen=True)
class TemplateFallbackConfig:
    zoom_reset_shortcut: str
    multi_scale: list[float]
    retry: int


@dataclass(frozen=True)
class AutomationConfig:
    driver: str
    weflow_exe: Path
    launch_timeout_sec: float
    poll_export_interval_sec: float
    window_geometry: WindowGeometry
    electron_accessibility_flag: str
    electron_cdp_port: int
    template_fallback: TemplateFallbackConfig


@dataclass(frozen=True)
class GroupContextWindowConfig:
    messages_before: int
    messages_after: int
    time_window_minutes: int
    anchor_keywords: list[str]


@dataclass(frozen=True)
class PreprocessingConfig:
    skip_emoji_dir: bool
    voice_fail_log_only: bool
    time_compress_interval_sec: int
    image_ocr_enabled: bool
    image_ocr_min_confidence: float
    image_ocr_max_inline_chars: int
    group_context_window: GroupContextWindowConfig


@dataclass(frozen=True)
class AgentConfig:
    cli: str
    model: str
    extra_args: list[str]


@dataclass(frozen=True)
class SkillsConfig:
    daily: list[str]


@dataclass(frozen=True)
class DailyExportConfig:
    target_usernames: list[str]
    target_processed_subroot: str
    cleanup_mode: str
    restart_weflow: bool


@dataclass(frozen=True)
class Config:
    user: UserConfig
    paths: PathsConfig
    automation: AutomationConfig
    preprocessing: PreprocessingConfig
    agent: AgentConfig
    skills: SkillsConfig
    daily_export: DailyExportConfig
    base_dir: Path
    raw: dict[str, Any]


def load_config(config_path: str | Path | None = None) -> Config:
    path = Path(config_path) if config_path is not None else Path("config.toml")
    base_dir = path.resolve().parent if path.exists() else Path.cwd().resolve()

    loaded: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as fh:
            loaded = tomllib.load(fh)

    merged = _deep_merge(DEFAULT_CONFIG, loaded)

    legacy_weflow_exe = merged.get("paths", {}).pop("weflow_exe", None)
    if legacy_weflow_exe and not loaded.get("automation", {}).get("weflow_exe"):
        merged["automation"]["weflow_exe"] = legacy_weflow_exe

    return _build_config(merged, base_dir)


def _build_config(raw: dict[str, Any], base_dir: Path) -> Config:
    paths = raw["paths"]
    automation = raw["automation"]
    preprocessing = raw["preprocessing"]
    group_window = preprocessing["group_context_window"]
    template = automation["template_fallback"]
    geometry = automation["window_geometry"]
    daily_export = raw["daily_export"]

    driver = str(automation["driver"]).strip().lower()
    if driver not in {"cdp", "uia", "template"}:
        raise ValueError(f"Unsupported automation driver: {driver}")
    cleanup_mode = str(daily_export.get("cleanup_mode") or "archive").strip().lower()
    if cleanup_mode not in {"archive", "delete", "skip"}:
        raise ValueError(f"Unsupported daily_export cleanup_mode: {cleanup_mode}")

    return Config(
        user=UserConfig(
            self_wxids=list(raw["user"]["self_wxids"]),
            voice_transcribe_usernames=list(raw["user"].get("voice_transcribe_usernames") or []),
        ),
        paths=PathsConfig(
            raw=_resolve_path(base_dir, paths["raw"]),
            processed=_resolve_path(base_dir, paths["processed"]),
            archived=_resolve_path(base_dir, paths["archived"]),
            insights=_resolve_path(base_dir, paths["insights"]),
            rotation_root=_resolve_path(base_dir, paths["rotation_root"]),
        ),
        automation=AutomationConfig(
            driver=driver,
            weflow_exe=_resolve_path(base_dir, automation["weflow_exe"]),
            launch_timeout_sec=float(automation["launch_timeout_sec"]),
            poll_export_interval_sec=float(automation["poll_export_interval_sec"]),
            window_geometry=WindowGeometry(
                width=int(geometry["width"]),
                height=int(geometry["height"]),
                x=_optional_int(geometry.get("x")),
                y=_optional_int(geometry.get("y")),
            ),
            electron_accessibility_flag=str(automation["electron_accessibility_flag"]),
            electron_cdp_port=int(automation["electron_cdp_port"]),
            template_fallback=TemplateFallbackConfig(
                zoom_reset_shortcut=str(template["zoom_reset_shortcut"]),
                multi_scale=[float(value) for value in template["multi_scale"]],
                retry=int(template["retry"]),
            ),
        ),
        preprocessing=PreprocessingConfig(
            skip_emoji_dir=bool(preprocessing["skip_emoji_dir"]),
            voice_fail_log_only=bool(preprocessing["voice_fail_log_only"]),
            time_compress_interval_sec=int(preprocessing["time_compress_interval_sec"]),
            image_ocr_enabled=bool(preprocessing["image_ocr_enabled"]),
            image_ocr_min_confidence=float(preprocessing["image_ocr_min_confidence"]),
            image_ocr_max_inline_chars=int(preprocessing["image_ocr_max_inline_chars"]),
            group_context_window=GroupContextWindowConfig(
                messages_before=int(group_window["messages_before"]),
                messages_after=int(group_window["messages_after"]),
                time_window_minutes=int(group_window["time_window_minutes"]),
                anchor_keywords=list(group_window.get("anchor_keywords") or []),
            ),
        ),
        agent=AgentConfig(
            cli=str(raw["agent"]["cli"]),
            model=str(raw["agent"]["model"]),
            extra_args=list(raw["agent"]["extra_args"]),
        ),
        skills=SkillsConfig(daily=list(raw["skills"]["daily"])),
        daily_export=DailyExportConfig(
            target_usernames=[str(value).strip() for value in daily_export.get("target_usernames") or [] if str(value).strip()],
            target_processed_subroot=str(daily_export.get("target_processed_subroot") or "_targets").strip() or "_targets",
            cleanup_mode=cleanup_mode,
            restart_weflow=bool(daily_export.get("restart_weflow", True)),
        ),
        base_dir=base_dir,
        raw=copy.deepcopy(raw),
    )


def _deep_merge(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _resolve_path(base_dir: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
