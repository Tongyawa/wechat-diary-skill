from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import copy
import sys
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
    },
    "export_backend": {
        "backend": "weflow_api",
        "weflow_api": {
            "base_url": "http://127.0.0.1:5031",
            "access_token": "",
            "media_localize": True,
            "message_format": "json",
            "request_timeout_sec": 120,
            "message_request_timeout_sec": 600,
            "appmsg_text_max_chars": 300,
        },
        "weflow": {
            "driver": "cdp",
            "weflow_exe": "C:/Path/To/WeFlow.exe",
            "launch_timeout_sec": 90,
            "poll_export_interval_sec": 60,
            # How long to tolerate an alive-but-unresponsive WeFlow (heavy background
            # export / the InsightService silent-contact scan pegs the single-threaded
            # renderer). Used twice: (1) keep retrying :9222 at connect time, and
            # (2) as the per-evaluate socket timeout so a transient renderer freeze
            # waits itself out instead of aborting a GUI step with a 10s socket
            # timeout (the moments date-range dialog failures). Generous on purpose:
            # WeFlow is busy, not dead.
            "cdp_busy_timeout_sec": 300,
            "window_geometry": {"width": 1280, "height": 900},
            "electron_accessibility_flag": "--force-renderer-accessibility",
            "electron_cdp_port": 9222,
            "template_fallback": {
                "zoom_reset_shortcut": "ctrl+0",
                "multi_scale": [0.85, 0.9, 0.95, 1.0, 1.05],
                "retry": 3,
            },
        },
    },
    "asr": {
        "engine": "",
        "model": "iic/SenseVoiceSmall",
        "language": "zh",
        "device": "cpu",
        "emit_emotion": True,
        "worker_python": "",
        "worker_script": "",
        "worker_startup_timeout_sec": 180,
        "worker_request_timeout_sec": 120,
    },
    "preprocessing": {
        "skip_emoji_dir": True,
        "voice_fail_log_only": True,
        "time_compress_interval_sec": 120,
        "image_ocr_enabled": True,
        "image_ocr_min_confidence": 0.55,
        "image_ocr_max_inline_chars": 80,
        "image_vision": {
            "enabled": False,
            "provider": "doubao",
            "model": "pro",
            "max_inline_chars": 1000,
            "concurrency": 4,
            "timeout_sec": 60,
            "max_tokens": 2000,
            "empty_retry_max_tokens": 16000,
            "cache_dir": "",
            "skip_usernames": [],
            "context_messages": 2,
            "anonymize_speakers": True,
            "include_moment_comments": False,
        },
        "group_context_window": {
            "enabled": True,
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
        "daily": ["wechat-diary-skill"],
    },
    "backup": {
        # Rolling git-bundle cold backup. Empty ``repos`` = feature disabled;
        # every check tied to it stays silent instead of warning.
        "bundle_dest": "",
        "keep": 5,
        "stale_warn_days": 3,
        "repos": [],
    },
    "daily_export": {
        "target_usernames": [],
        "skip_official_accounts": True,
        # None = key absent in config.toml (never answered); [] = explicit opt-out.
        "self_moments_usernames": None,
        "target_processed_subroot": "_targets",
        "voice_fallback_script": "",
        "cleanup_mode": "archive",
        "restart_weflow": True,
    },
}


_MIGRATION_HINTED_PATHS: set[Path] = set()


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
    cdp_busy_timeout_sec: float
    window_geometry: WindowGeometry
    electron_accessibility_flag: str
    electron_cdp_port: int
    template_fallback: TemplateFallbackConfig


@dataclass(frozen=True)
class WeflowApiConfig:
    base_url: str
    access_token: str
    media_localize: bool
    message_format: str
    request_timeout_sec: float
    appmsg_text_max_chars: int = 300
    message_request_timeout_sec: float = 600


@dataclass(frozen=True)
class ExportBackendConfig:
    backend: str
    weflow: AutomationConfig
    weflow_api: WeflowApiConfig


@dataclass(frozen=True)
class AsrConfig:
    engine: str
    model: str
    language: str
    device: str
    emit_emotion: bool
    worker_python: Path | None
    worker_script: Path | None
    worker_startup_timeout_sec: float
    worker_request_timeout_sec: float


@dataclass(frozen=True)
class GroupContextWindowConfig:
    enabled: bool
    messages_before: int
    messages_after: int
    time_window_minutes: int
    anchor_keywords: list[str]


@dataclass(frozen=True)
class ImageVisionConfig:
    enabled: bool
    provider: str
    model: str
    max_inline_chars: int
    concurrency: int
    timeout_sec: float
    max_tokens: int
    empty_retry_max_tokens: int
    cache_dir: Path | None
    skip_usernames: list[str]
    context_messages: int
    anonymize_speakers: bool
    include_moment_comments: bool


@dataclass(frozen=True)
class PreprocessingConfig:
    skip_emoji_dir: bool
    voice_fail_log_only: bool
    time_compress_interval_sec: int
    image_ocr_enabled: bool
    image_ocr_min_confidence: float
    image_ocr_max_inline_chars: int
    image_vision: ImageVisionConfig
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
class BackupRepo:
    """One git repository to snapshot into a rolling bundle."""

    name: str
    path: Path


@dataclass(frozen=True)
class BackupConfig:
    """Rolling git-bundle cold backup.

    ``enabled`` is derived, not configured: the feature is on exactly when a
    destination and at least one repo are configured. Callers must stay silent
    when it is off — an unconfigured optional feature is not a problem to report.
    """

    bundle_dest: Path | None
    keep: int
    stale_warn_days: int
    repos: list[BackupRepo]
    #: Config errors found while parsing ``repos``. Never silently dropped --
    #: a repo the user meant to back up but that got discarded is precisely the
    #: "believed backed up, actually not" failure this feature exists to prevent.
    problems: list[str] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.bundle_dest is not None and bool(self.repos)

    @property
    def configured(self) -> bool:
        """True when the user tried to configure backups at all.

        Distinct from ``enabled``: a section whose every entry is malformed is
        configured-but-unusable, and must warn rather than stay silent.
        """
        return self.enabled or bool(self.problems)

    @property
    def state_file(self) -> Path | None:
        """Where the orchestrator records the outcome of each run."""
        return None if self.bundle_dest is None else self.bundle_dest / "last-run.json"


@dataclass(frozen=True)
class DailyExportConfig:
    target_usernames: list[str]
    skip_official_accounts: bool
    self_moments_usernames: list[str]
    # False when the key is absent from config.toml; lets the runner tell
    # "never configured" apart from "deliberately disabled with []".
    self_moments_configured: bool
    target_processed_subroot: str
    voice_fallback_script: Path | None
    cleanup_mode: str
    restart_weflow: bool


@dataclass(frozen=True)
class Config:
    user: UserConfig
    paths: PathsConfig
    export_backend: ExportBackendConfig
    # Compatibility alias for callers not yet migrated to
    # ``config.export_backend.weflow``.
    automation: AutomationConfig
    asr: AsrConfig
    preprocessing: PreprocessingConfig
    agent: AgentConfig
    skills: SkillsConfig
    backup: BackupConfig
    daily_export: DailyExportConfig
    base_dir: Path
    raw: dict[str, Any]
    # Values explicitly present in config.toml, before defaults are merged.
    source: dict[str, Any]


def load_config(config_path: str | Path | None = None) -> Config:
    path = Path(config_path) if config_path is not None else Path("config.toml")
    base_dir = path.resolve().parent if path.exists() else Path.cwd().resolve()

    loaded: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as fh:
            loaded = tomllib.load(fh)

    normalized = _normalize_export_backend(loaded, path)
    merged = _deep_merge(DEFAULT_CONFIG, normalized)

    legacy_weflow_exe = merged.get("paths", {}).pop("weflow_exe", None)
    explicit_weflow = (normalized.get("export_backend") or {}).get("weflow") or {}
    if legacy_weflow_exe and not explicit_weflow.get("weflow_exe"):
        merged["export_backend"]["weflow"]["weflow_exe"] = legacy_weflow_exe

    return _build_config(merged, base_dir, source=loaded)


def _build_config(raw: dict[str, Any], base_dir: Path, *, source: dict[str, Any]) -> Config:
    paths = raw["paths"]
    export_backend = raw["export_backend"]
    weflow_api = export_backend["weflow_api"]
    automation = export_backend["weflow"]
    asr = raw["asr"]
    preprocessing = raw["preprocessing"]
    image_vision = preprocessing["image_vision"]
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

    backend_name = str(export_backend.get("backend") or "weflow_api").strip().lower()
    if not backend_name:
        raise ValueError("export_backend.backend must not be empty")

    message_format = str(weflow_api.get("message_format") or "json").strip().lower()
    if message_format != "json":
        raise ValueError("export_backend.weflow_api.message_format 本期只支持 json")
    appmsg_text_max_chars = int(weflow_api.get("appmsg_text_max_chars", 300))
    if appmsg_text_max_chars < 1:
        raise ValueError("export_backend.weflow_api.appmsg_text_max_chars 必须大于等于 1")
    asr_engine = str(asr.get("engine") or "").strip().lower()
    if asr_engine not in {"", "sensevoice", "whisper"}:
        raise ValueError(f"Unsupported ASR engine: {asr_engine}")
    worker_startup_timeout_sec = float(asr.get("worker_startup_timeout_sec", 180))
    worker_request_timeout_sec = float(asr.get("worker_request_timeout_sec", 120))
    if worker_startup_timeout_sec <= 0 or worker_request_timeout_sec <= 0:
        raise ValueError("asr worker timeout 必须大于 0 秒")
    image_vision_max_inline_chars = int(image_vision.get("max_inline_chars", 1000))
    image_vision_concurrency = int(image_vision.get("concurrency", 4))
    image_vision_timeout_sec = float(image_vision.get("timeout_sec", 60))
    image_vision_max_tokens = int(image_vision.get("max_tokens", 2000))
    image_vision_retry_tokens = int(image_vision.get("empty_retry_max_tokens", 16000))
    image_vision_context_messages = int(image_vision.get("context_messages", 2))
    if image_vision_max_inline_chars < 1:
        raise ValueError("preprocessing.image_vision.max_inline_chars 必须大于等于 1")
    if image_vision_concurrency < 1:
        raise ValueError("preprocessing.image_vision.concurrency 必须大于等于 1")
    if image_vision_timeout_sec <= 0:
        raise ValueError("preprocessing.image_vision.timeout_sec 必须大于 0 秒")
    if image_vision_max_tokens < 1 or image_vision_retry_tokens < 1:
        raise ValueError("preprocessing.image_vision token 预算必须大于 0")
    if image_vision_context_messages < 0:
        raise ValueError("preprocessing.image_vision.context_messages 不得小于 0")
    explicit_vision = ((source.get("preprocessing") or {}).get("image_vision") or {})
    image_vision_provider = str(image_vision.get("provider") or "doubao").strip()
    image_vision_model = str(image_vision.get("model") or "pro").strip()
    if "max_tokens" not in explicit_vision and (
        image_vision_provider.casefold() == "jisuan" and "qwen" in image_vision_model.casefold()
    ):
        image_vision_max_tokens = 8000
    if (
        image_vision_provider.casefold() == "jisuan"
        and "qwen" in image_vision_model.casefold()
        and image_vision_max_tokens < 8000
    ):
        raise ValueError("jisuan/qwen 系的 preprocessing.image_vision.max_tokens 不得低于 8000")

    automation_config = AutomationConfig(
        driver=driver,
        weflow_exe=_resolve_path(base_dir, automation["weflow_exe"]),
        launch_timeout_sec=float(automation["launch_timeout_sec"]),
        poll_export_interval_sec=float(automation["poll_export_interval_sec"]),
        cdp_busy_timeout_sec=float(automation["cdp_busy_timeout_sec"]),
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
    )

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
        ),
        export_backend=ExportBackendConfig(
            backend=backend_name,
            weflow=automation_config,
            weflow_api=WeflowApiConfig(
                base_url=str(weflow_api.get("base_url") or "http://127.0.0.1:5031").rstrip("/"),
                access_token=str(weflow_api.get("access_token") or "").strip(),
                media_localize=bool(weflow_api.get("media_localize", True)),
                message_format=message_format,
                request_timeout_sec=float(weflow_api.get("request_timeout_sec", 120)),
                appmsg_text_max_chars=appmsg_text_max_chars,
                message_request_timeout_sec=float(
                    weflow_api.get("message_request_timeout_sec", 600)
                ),
            ),
        ),
        automation=automation_config,
        asr=AsrConfig(
            engine=asr_engine,
            model=str(asr.get("model") or "iic/SenseVoiceSmall"),
            language=str(asr.get("language") or "zh"),
            device=str(asr.get("device") or "cpu"),
            emit_emotion=bool(asr.get("emit_emotion", True)),
            worker_python=_optional_path(base_dir, asr.get("worker_python")),
            worker_script=_optional_path(base_dir, asr.get("worker_script")),
            worker_startup_timeout_sec=worker_startup_timeout_sec,
            worker_request_timeout_sec=worker_request_timeout_sec,
        ),
        preprocessing=PreprocessingConfig(
            skip_emoji_dir=bool(preprocessing["skip_emoji_dir"]),
            voice_fail_log_only=bool(preprocessing["voice_fail_log_only"]),
            time_compress_interval_sec=int(preprocessing["time_compress_interval_sec"]),
            image_ocr_enabled=bool(preprocessing["image_ocr_enabled"]),
            image_ocr_min_confidence=float(preprocessing["image_ocr_min_confidence"]),
            image_ocr_max_inline_chars=int(preprocessing["image_ocr_max_inline_chars"]),
            image_vision=ImageVisionConfig(
                enabled=bool(image_vision.get("enabled", False)),
                provider=image_vision_provider,
                model=image_vision_model,
                max_inline_chars=image_vision_max_inline_chars,
                concurrency=image_vision_concurrency,
                timeout_sec=image_vision_timeout_sec,
                max_tokens=image_vision_max_tokens,
                empty_retry_max_tokens=image_vision_retry_tokens,
                cache_dir=_optional_path(base_dir, image_vision.get("cache_dir")),
                skip_usernames=[
                    str(value).strip()
                    for value in image_vision.get("skip_usernames") or []
                    if str(value).strip()
                ],
                context_messages=image_vision_context_messages,
                anonymize_speakers=bool(image_vision.get("anonymize_speakers", True)),
                include_moment_comments=bool(image_vision.get("include_moment_comments", False)),
            ),
            group_context_window=GroupContextWindowConfig(
                enabled=bool(group_window["enabled"]),
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
        backup=_build_backup_config(raw.get("backup") or {}, base_dir),
        daily_export=DailyExportConfig(
            target_usernames=[str(value).strip() for value in daily_export.get("target_usernames") or [] if str(value).strip()],
            skip_official_accounts=bool(daily_export.get("skip_official_accounts", True)),
            self_moments_usernames=[
                str(value).strip() for value in daily_export.get("self_moments_usernames") or [] if str(value).strip()
            ],
            self_moments_configured=daily_export.get("self_moments_usernames") is not None,
            target_processed_subroot=str(daily_export.get("target_processed_subroot") or "_targets").strip() or "_targets",
            voice_fallback_script=_optional_path(base_dir, daily_export.get("voice_fallback_script")),
            cleanup_mode=cleanup_mode,
            restart_weflow=bool(daily_export.get("restart_weflow", True)),
        ),
        base_dir=base_dir,
        raw=copy.deepcopy(raw),
        source=copy.deepcopy(source),
    )


def _normalize_export_backend(loaded: dict[str, Any], path: Path) -> dict[str, Any]:
    """Map legacy ``[automation]`` values into the new backend namespace."""

    normalized = copy.deepcopy(loaded)
    legacy = loaded.get("automation")
    if not isinstance(legacy, dict):
        return normalized

    export_backend = normalized.setdefault("export_backend", {})
    # A legacy-only config must keep selecting the legacy adapter even though
    # fresh configs now default to the HTTP API backend.
    export_backend.setdefault("backend", "weflow")
    explicit_weflow = export_backend.get("weflow")
    if isinstance(explicit_weflow, dict):
        export_backend["weflow"] = _deep_merge(legacy, explicit_weflow)
    else:
        export_backend["weflow"] = copy.deepcopy(legacy)
    normalized.pop("automation", None)

    resolved = path.resolve()
    if resolved not in _MIGRATION_HINTED_PATHS:
        print(
            "config 建议迁移到 [export_backend.weflow]；当前 [automation] 仍兼容。"
            "请将 config.toml 的 [automation] / [automation.template_fallback] 段名改成 "
            "[export_backend.weflow] / [export_backend.weflow.template_fallback] 即可消除本提示。",
            file=sys.stderr,
        )
        _MIGRATION_HINTED_PATHS.add(resolved)
    return normalized


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


def _optional_path(base_dir: Path, value: Any) -> Path | None:
    text = str(value or "").strip()
    return _resolve_path(base_dir, text) if text else None


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


#: A bundle name becomes a filename prefix, so it must not carry path
#: separators or characters Windows rejects.
_ILLEGAL_NAME_CHARS = set('\\/:*?"<>|')


def _build_backup_config(raw: dict[str, Any], base_dir: Path) -> BackupConfig:
    """Parse ``[backup]``, collecting problems instead of silently dropping entries.

    Nothing here raises: ``load_config`` backs every entry point, and a malformed
    *backup* section must not stop ``doctor.py`` from running -- that is exactly
    when you need the diagnosis. Problems are carried on the config and surfaced
    by every consumer; the orchestrator refuses to run while any exist.

    Silence is the one thing this must never do: a dropped or overwritten repo
    means a backup the user believes exists and does not.
    """
    repos: list[BackupRepo] = []
    problems: list[str] = []
    seen: dict[str, tuple[int, str]] = {}

    for index, entry in enumerate(raw.get("repos") or [], start=1):
        label = f"[backup].repos 第 {index} 项"
        if not isinstance(entry, dict):
            problems.append(f"{label}：不是一个表（应形如 {{ name = \"...\", path = \"...\" }}）。")
            continue

        path_text = str(entry.get("path") or "").strip()
        if not path_text:
            problems.append(f"{label}：缺少 path，无法备份。补上 path，或整条删掉。")
            continue
        path = _resolve_path(base_dir, path_text)

        # Default the bundle name to the repo directory's leaf, matching
        # Backup-GitRepo.ps1's own default so both entry points agree.
        name = str(entry.get("name") or "").strip() or path.name
        if not name:
            problems.append(f"{label}：name 为空且无法从 path 推断，请显式指定。")
            continue

        illegal = sorted(set(name) & _ILLEGAL_NAME_CHARS)
        if illegal:
            problems.append(
                f"{label}：name「{name}」含不能作为文件名的字符 {''.join(illegal)}。"
            )
            continue

        # Case-insensitively: the name becomes a filename, and Windows treats
        # "Collision" and "collision" as the same file. A case-sensitive check
        # would let the pair through and silently overwrite one backup.
        key = name.casefold()
        if key in seen:
            prior_index, prior_name = seen[key]
            same_case = "" if prior_name == name else "（仅大小写不同，Windows 视为同一文件）"
            problems.append(
                f"{label}：name「{name}」与第 {prior_index} 项「{prior_name}」重复{same_case}，"
                f"两者会写同一个 bundle 文件、后者覆盖前者。请改成唯一名字。"
            )
            continue
        seen[key] = (index, name)

        repos.append(BackupRepo(name=name, path=path))

    bundle_dest = _optional_path(base_dir, raw.get("bundle_dest"))
    raw_repos = raw.get("repos") or []
    if bundle_dest is not None and not raw_repos:
        # A destination with nothing to put in it. Reading this as "disabled"
        # would exit 0 nightly while the user believes backups are running --
        # the same believed-safe-but-isn't failure as every other case here.
        problems.append(
            "[backup] 配置了 bundle_dest 却没有 repos，不会备份任何东西。"
            "补上 repos，或整段删掉以显式关闭。"
        )
    if repos and bundle_dest is None:
        # Repos configured but nowhere to put the bundles. Without this the
        # config reads as "disabled" and the job exits 0 -- the user believes
        # these repos are backed up nightly and nothing is being written.
        problems.append(
            "[backup] 配置了 repos 却缺少 bundle_dest，bundle 无处可落。"
            "补上 bundle_dest（建议指向随云同步的目录），或整段删掉以显式关闭。"
        )

    return BackupConfig(
        bundle_dest=bundle_dest,
        keep=max(1, int(raw.get("keep", 5) or 5)),
        stale_warn_days=max(1, int(raw.get("stale_warn_days", 3) or 3)),
        repos=repos,
        problems=problems,
    )
