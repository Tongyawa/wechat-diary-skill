from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_diary_core.config import Config, load_config
from wechat_diary_core.asr import default_worker_script
from wechat_diary_core.backends.weflow.cdp_driver import fetch_cdp_targets
from wechat_diary_core.backends.weflow_api.client import WeflowApiClient


STATUS_ICON = {"ready": "✅", "warning": "⚠️", "error": "❌"}
STATUS_LABEL = {"ready": "就绪", "warning": "注意", "error": "缺失"}
REQUIRED_PATH_KEYS = (
    "paths.raw",
    "paths.processed",
    "paths.archived",
    "paths.insights",
)


@dataclass(frozen=True)
class CheckResult:
    id: str
    group: str
    name: str
    status: str
    message: str
    action: str | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class DoctorReport:
    checks: list[CheckResult]

    def to_dict(self) -> dict[str, Any]:
        counts = Counter(check.status for check in self.checks)
        return {
            "checks": [asdict(check) for check in self.checks],
            "summary": {
                "ready": counts["ready"],
                "warning": counts["warning"],
                "error": counts["error"],
                "can_run_daily_export": counts["error"] == 0,
                "conclusion": _conclusion(counts),
            },
        }


@dataclass(frozen=True)
class ProbeDependencies:
    load_config: Callable[[str | Path | None], Config] = load_config
    fetch_cdp_targets: Callable[[str], list[dict[str, Any]]] = fetch_cdp_targets
    find_spec: Callable[[str], Any] = importlib.util.find_spec
    can_write: Callable[[Path], bool] = lambda path: os.access(path, os.W_OK)
    api_client_factory: Callable[[Config], Any] = lambda cfg: WeflowApiClient(
        cfg.export_backend.weflow_api.base_url,
        cfg.export_backend.weflow_api.access_token,
        timeout=cfg.export_backend.weflow_api.request_timeout_sec,
        message_timeout=cfg.export_backend.weflow_api.message_request_timeout_sec,
    )


def run_doctor(config_path: str | Path = "config.toml", *, deps: ProbeDependencies | None = None) -> DoctorReport:
    active = deps or ProbeDependencies()
    path = Path(config_path)
    if not path.exists():
        return DoctorReport(
            [
                CheckResult(
                    id="config",
                    group="配置",
                    name="config.toml",
                    status="error",
                    message=f"未找到配置文件：{path}",
                    action="复制 config.example.toml 为 config.toml，再填写本机路径。",
                ),
                *_config_blocked_checks(),
            ]
        )

    try:
        cfg = active.load_config(path)
    except Exception as exc:
        return DoctorReport(
            [
                CheckResult(
                    id="config",
                    group="配置",
                    name="config.toml",
                    status="error",
                    message=f"配置无法解析：{exc}",
                    action="对照 config.example.toml 修正 config.toml 后重试。",
                ),
                *_config_blocked_checks(),
            ]
        )

    checks = [_check_config(cfg, path)]
    if cfg.export_backend.backend == "manual":
        checks.extend(_manual_backend_checks())
    elif cfg.export_backend.backend == "weflow":
        checks.append(_check_weflow_executable(cfg))
        checks.append(_check_cdp(cfg, active.fetch_cdp_targets))
    elif cfg.export_backend.backend == "weflow_api":
        client = active.api_client_factory(cfg)
        checks.append(_check_message_request_timeout(cfg))
        checks.append(_check_api_health(cfg, client))
        checks.append(_check_api_token(cfg))
        checks.append(_check_api_semantic(cfg, client))
        checks.append(_check_asr(cfg))
    checks.extend(_check_data_roots(cfg, active.can_write))
    checks.extend(_check_optional_dependencies(cfg, active.find_spec))
    return DoctorReport(checks)


def _check_config(cfg: Config, config_path: Path) -> CheckResult:
    missing = [key for key in REQUIRED_PATH_KEYS if not _has_config_key(cfg.source, key)]
    if cfg.export_backend.backend == "weflow":
        for key in ("weflow_exe", "electron_cdp_port"):
            if not (
                _has_config_key(cfg.source, f"export_backend.weflow.{key}")
                or _has_config_key(cfg.source, f"automation.{key}")
            ):
                missing.append(f"export_backend.weflow.{key}")
    elif cfg.export_backend.backend == "weflow_api" and not _has_config_key(
        cfg.source, "export_backend.weflow_api.base_url"
    ):
        missing.append("export_backend.weflow_api.base_url")
    if missing:
        return CheckResult(
            id="config",
            group="配置",
            name="config.toml",
            status="error",
            message="配置文件可解析，但缺少关键键：" + "、".join(missing),
            action="对照 config.example.toml 补齐上述键；不要依赖默认值掩盖本机配置缺失。",
            details={"path": str(config_path.resolve()), "missing_keys": missing},
        )
    return CheckResult(
        id="config",
        group="配置",
        name="config.toml",
        status="ready",
        message="配置文件可解析，关键键齐全。",
        details={"path": str(config_path.resolve())},
    )


def _has_config_key(source: dict[str, Any], dotted_key: str) -> bool:
    value: Any = source
    for part in dotted_key.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return value not in (None, "")


def _check_message_request_timeout(cfg: Config) -> CheckResult:
    api_source = cfg.source.get("export_backend", {}).get("weflow_api", {})
    explicit = isinstance(api_source, dict) and "message_request_timeout_sec" in api_source
    timeout = cfg.export_backend.weflow_api.message_request_timeout_sec
    if explicit and timeout < 300:
        return CheckResult(
            id="weflow_api_message_timeout",
            group="配置",
            name="消息数据请求超时",
            status="warning",
            message=(
                f"message_request_timeout_sec 当前显式设为 {timeout:g} 秒；"
                "媒体密集会话真机实测需 104 秒以上，冷缓存时容易超时。"
            ),
            action=(
                "把 config.toml 的 [export_backend.weflow_api]."
                "message_request_timeout_sec 调到 600–900 后重试。"
            ),
            details={"explicit": True, "seconds": timeout},
        )
    return CheckResult(
        id="weflow_api_message_timeout",
        group="配置",
        name="消息数据请求超时",
        status="ready",
        message=f"消息数据请求超时为 {timeout:g} 秒，可覆盖已知媒体密集会话。",
        details={"explicit": explicit, "seconds": timeout},
    )


def _check_weflow_executable(cfg: Config) -> CheckResult:
    executable = cfg.automation.weflow_exe
    if executable.is_file():
        return CheckResult(
            id="weflow_executable",
            group="数据入口",
            name="WeFlow 可执行文件",
            status="ready",
            message=f"文件存在：{executable}",
            details={"path": str(executable)},
        )
    return CheckResult(
        id="weflow_executable",
        group="数据入口",
        name="WeFlow 可执行文件",
        status="error",
        message=f"文件不存在：{executable}",
        action="修改 config.toml 的 export_backend.weflow.weflow_exe，使其指向实际的 WeFlow.exe。",
        details={"path": str(executable)},
    )


def _check_cdp(cfg: Config, probe: Callable[[str], list[dict[str, Any]]]) -> CheckResult:
    port = cfg.automation.electron_cdp_port
    endpoint = f"http://127.0.0.1:{port}"
    if not 1 <= port <= 65535:
        return CheckResult(
            id="cdp",
            group="数据入口",
            name="CDP 调试端口",
            status="error",
            message=f"端口值无效：{port}",
            action="把 config.toml 的 export_backend.weflow.electron_cdp_port 改为 1–65535 之间的端口。",
            details={"endpoint": endpoint},
        )
    try:
        targets = probe(endpoint)
    except Exception as exc:
        return CheckResult(
            id="cdp",
            group="数据入口",
            name="CDP 调试端口",
            status="warning",
            message=f"当前无法连接 {endpoint}。",
            action="WeFlow 未运行；双击 Start-DailyExport.bat 会自动拉起，或手动打开 WeFlow。",
            details={"endpoint": endpoint, "target_count": 0, "error": str(exc)},
        )
    if not targets:
        return CheckResult(
            id="cdp",
            group="数据入口",
            name="CDP 调试端口",
            status="warning",
            message=f"已连接 {endpoint}，但没有发现可用页面目标。",
            action="等待 WeFlow 主界面加载完成后重试；若仍为空，重新运行 Start-DailyExport.bat。",
            details={"endpoint": endpoint, "target_count": 0},
        )
    return CheckResult(
        id="cdp",
        group="数据入口",
        name="CDP 调试端口",
        status="ready",
        message=f"已连接 {endpoint}，发现 {len(targets)} 个目标。",
        details={"endpoint": endpoint, "target_count": len(targets)},
    )


def _manual_backend_checks() -> list[CheckResult]:
    return [
        CheckResult(
            id="weflow_executable",
            group="数据入口",
            name="WeFlow 可执行文件",
            status="ready",
            message="manual 后端不需要 WeFlow 可执行文件。",
        ),
        CheckResult(
            id="cdp",
            group="数据入口",
            name="CDP 调试端口",
            status="ready",
            message="manual 后端不连接 CDP；将直接处理现有 raw。",
        ),
    ]


def _check_api_health(cfg: Config, client: Any) -> CheckResult:
    endpoint = cfg.export_backend.weflow_api.base_url
    try:
        client.health()
    except Exception as exc:
        return CheckResult(
            id="weflow_api_health",
            group="数据入口",
            name="WeFlow API 探活",
            status="error",
            message=f"无法通过 {endpoint}/health 探活。",
            action="打开 WeFlow → 设置 → API 服务 → 启动服务；首次启用必须手动操作一次。",
            details={"endpoint": endpoint, "error": str(exc)},
        )
    return CheckResult(
        id="weflow_api_health",
        group="数据入口",
        name="WeFlow API 探活",
        status="ready",
        message=f"{endpoint}/health 可达。",
        details={"endpoint": endpoint},
    )


def _check_api_token(cfg: Config) -> CheckResult:
    configured = bool(cfg.export_backend.weflow_api.access_token)
    if configured:
        return CheckResult(
            id="weflow_api_token",
            group="数据入口",
            name="WeFlow API Token",
            status="ready",
            message="已配置固定非空 Access Token（内容不回显）。",
            details={"configured": True},
        )
    return CheckResult(
        id="weflow_api_token",
        group="数据入口",
        name="WeFlow API Token",
        status="error",
        message="Access Token 未配置。",
        action="在 WeFlow API 服务中生成固定 token，写入 config.toml；修改后重启 API 服务。",
        details={"configured": False},
    )


def _check_api_semantic(cfg: Config, client: Any) -> CheckResult:
    if not cfg.export_backend.weflow_api.access_token:
        return CheckResult(
            id="weflow_api_semantic",
            group="数据入口",
            name="WeFlow API 语义探测",
            status="warning",
            message="因 Access Token 未配置而未探测受保护消息端点。",
            action="先配置固定 token，再重跑 doctor。",
        )
    try:
        talker, count = client.semantic_probe()
    except Exception as exc:
        return CheckResult(
            id="weflow_api_semantic",
            group="数据入口",
            name="WeFlow API 语义探测",
            status="error",
            message="health 可用不代表数据可读；当前未能读取已知非空会话。",
            action="确认 token 匹配并已重启 API 服务；若仍为空，检查当前微信数据版本是否受 WeFlow 支持。",
            details={"error": str(exc)},
        )
    return CheckResult(
        id="weflow_api_semantic",
        group="数据入口",
        name="WeFlow API 语义探测",
        status="ready",
        message="受保护消息端点可读，已找到非空会话。",
        details={"talker": talker, "sample_count": count},
    )


def _check_asr(cfg: Config) -> CheckResult:
    engine = cfg.asr.engine
    if not engine:
        return CheckResult(
            id="dependency_asr",
            group="可选能力",
            name="SenseVoice 本地语音转写",
            status="warning",
            message="ASR 未启用；语音会写入可识别的“转文字失败”占位。",
            details={"engine": "", "enabled": False},
        )
    if engine == "whisper":
        return CheckResult(
            id="dependency_asr",
            group="可选能力",
            name="本地语音转写",
            status="warning",
            message="whisper 是保留值，本期未实现；语音将优雅降级。",
            action="本期请使用 engine = \"sensevoice\"，或设为空字符串关闭。",
            details={"engine": engine, "enabled": False},
        )
    worker_python = cfg.asr.worker_python
    if worker_python is None:
        return CheckResult(
            id="dependency_asr",
            group="可选能力",
            name="SenseVoice 常驻 worker",
            status="warning",
            message="可选能力未就绪：[asr].worker_python 未配置。",
            action=(
                "在独立 uv 项目中安装 SenseVoice，再把该项目 .venv 的 Python 绝对路径写入 "
                "[asr].worker_python；不要向 daily export 使用的全局 Python 安装依赖。"
            ),
            details={"engine": engine, "worker_python": ""},
        )
    python_executable = worker_python.is_file() and (os.name == "nt" or os.access(worker_python, os.X_OK))
    if not python_executable:
        return CheckResult(
            id="dependency_asr",
            group="可选能力",
            name="SenseVoice 常驻 worker",
            status="warning",
            message="可选能力未就绪：worker_python 不存在或不可执行。",
            action="修正 [asr].worker_python，使其指向独立 uv 环境内可执行的 Python。",
            details={"engine": engine, "worker_python": str(worker_python)},
        )
    worker_script = cfg.asr.worker_script or default_worker_script()
    if not worker_script.is_file():
        return CheckResult(
            id="dependency_asr",
            group="可选能力",
            name="SenseVoice 常驻 worker",
            status="warning",
            message="可选能力未就绪：worker_script 不存在。",
            action="将 [asr].worker_script 留空以使用仓库自带脚本，或配置有效的绝对路径。",
            details={"engine": engine, "worker_script": str(worker_script)},
        )
    return CheckResult(
        id="dependency_asr",
        group="可选能力",
        name="SenseVoice 常驻 worker",
        status="ready",
        message="独立 Python 与 worker 脚本均已配置；模型将在首次语音时懒加载。",
        details={
            "engine": engine,
            "model": cfg.asr.model,
            "worker_python": str(worker_python),
            "worker_script": str(worker_script),
        },
    )


def _check_data_roots(cfg: Config, can_write: Callable[[Path], bool]) -> list[CheckResult]:
    roots = (
        ("raw", "原始导出目录", cfg.paths.raw, False),
        ("processed", "加工结果目录", cfg.paths.processed, False),
        ("archived", "长期归档目录", cfg.paths.archived, _source_path_is_absolute(cfg, "archived")),
        ("insights", "洞察产物目录", cfg.paths.insights, _source_path_is_absolute(cfg, "insights")),
    )
    return [_check_data_root(key, name, path, remote_absolute, can_write) for key, name, path, remote_absolute in roots]


def _source_path_is_absolute(cfg: Config, key: str) -> bool:
    value = cfg.source.get("paths", {}).get(key)
    return isinstance(value, str) and Path(value).is_absolute()


def _check_data_root(
    key: str,
    name: str,
    path: Path,
    remote_absolute: bool,
    can_write: Callable[[Path], bool],
) -> CheckResult:
    details = {"path": str(path), "remote_absolute": remote_absolute}
    if path.is_dir() and can_write(path):
        return CheckResult(
            id=f"path_{key}",
            group="数据目录",
            name=name,
            status="ready",
            message=f"目录存在且可写：{path}",
            details=details,
        )

    if path.exists() and not path.is_dir():
        message = f"配置路径不是目录：{path}"
    elif not path.exists():
        message = f"目录不存在：{path}"
    else:
        message = f"目录不可写：{path}"

    if remote_absolute:
        action = "持久数据根不可达；确认夸克网盘同步在运行、盘符正确，并检查目录权限。"
    else:
        action = f"确认 config.toml 的 paths.{key} 正确，并预先创建该目录或修复写入权限。"
    return CheckResult(
        id=f"path_{key}",
        group="数据目录",
        name=name,
        status="error",
        message=message,
        action=action,
        details=details,
    )


def _check_optional_dependencies(cfg: Config, find_spec: Callable[[str], Any]) -> list[CheckResult]:
    checks: list[CheckResult] = []
    if cfg.preprocessing.image_ocr_enabled:
        candidates = ("rapidocr_onnxruntime", "rapidocr", "paddleocr")
        available = next((name for name in candidates if _module_available(name, find_spec)), None)
        if available:
            checks.append(
                CheckResult(
                    id="dependency_ocr",
                    group="可选能力",
                    name="图片 OCR",
                    status="ready",
                    message=f"已启用，依赖可用：{available}",
                    details={"enabled": True, "module": available},
                )
            )
        else:
            checks.append(
                CheckResult(
                    id="dependency_ocr",
                    group="可选能力",
                    name="图片 OCR",
                    status="error",
                    message="已启用，但未找到 RapidOCR 或 PaddleOCR 依赖。",
                    action="运行 python -m pip install -r requirements.txt 安装 OCR 依赖。",
                    details={"enabled": True, "modules_checked": list(candidates)},
                )
            )
    else:
        checks.append(
            CheckResult(
                id="dependency_ocr",
                group="可选能力",
                name="图片 OCR",
                status="warning",
                message="未启用，已跳过依赖探测。",
                details={"enabled": False},
            )
        )

    if cfg.user.voice_transcribe_usernames:
        checks.append(
            CheckResult(
                id="dependency_voice_transcribe",
                group="可选能力",
                name="WeFlow 内置语音转写",
                status="ready",
                message="已配置；该能力由 WeFlow 提供，不需要额外 Python 依赖。",
                details={"enabled": True},
            )
        )
    else:
        checks.append(
            CheckResult(
                id="dependency_voice_transcribe",
                group="可选能力",
                name="WeFlow 内置语音转写",
                status="warning",
                message="未配置联系人，已跳过该能力。",
                details={"enabled": False},
            )
        )

    fallback = cfg.daily_export.voice_fallback_script
    if fallback is not None:
        if fallback.is_file():
            checks.append(
                CheckResult(
                    id="dependency_voice_fallback",
                    group="可选能力",
                    name="语音 fallback",
                    status="ready",
                    message=f"脚本存在：{fallback}",
                    details={"enabled": True, "path": str(fallback)},
                )
            )
        else:
            checks.append(
                CheckResult(
                    id="dependency_voice_fallback",
                    group="可选能力",
                    name="语音 fallback",
                    status="error",
                    message=f"已配置，但脚本不存在：{fallback}",
                    action="修改 config.toml 的 daily_export.voice_fallback_script，或将其设为空字符串以关闭。",
                    details={"enabled": True, "path": str(fallback)},
                )
            )
    return checks


def _module_available(name: str, find_spec: Callable[[str], Any]) -> bool:
    try:
        return find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _config_blocked_checks() -> list[CheckResult]:
    action = "先准备可解析的 config.toml，再重新运行 doctor。"
    return [
        CheckResult("weflow_executable", "数据入口", "WeFlow 可执行文件", "warning", "因配置不可用而未检查。", action),
        CheckResult("cdp", "数据入口", "CDP 调试端口", "warning", "因配置不可用而未检查。", action),
        CheckResult("data_roots", "数据目录", "数据根", "warning", "因配置不可用而未检查。", action),
        CheckResult("optional_dependencies", "可选能力", "可选依赖", "warning", "因配置不可用而未检查。", action),
    ]


def _conclusion(counts: Counter[str]) -> str:
    if counts["error"]:
        return f"有 {counts['error']} 项待处理"
    if counts["warning"]:
        return f"可以跑每日导出（有 {counts['warning']} 项注意）"
    return "可以跑每日导出"


def print_human_report(report: DoctorReport) -> None:
    current_group: str | None = None
    for check in report.checks:
        if check.group != current_group:
            if current_group is not None:
                print()
            current_group = check.group
            print(f"[{current_group}]")
        print(f"{STATUS_ICON[check.status]} {STATUS_LABEL[check.status]} · {check.name}：{check.message}")
        if check.action:
            print(f"   下一步：{check.action}")
    print()
    print(report.to_dict()["summary"]["conclusion"])


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="只读检查 WeChat Diary 当前数据入口与可选能力。")
    parser.add_argument("--config", default="config.toml", help="config.toml 路径。")
    parser.add_argument("--json", action="store_true", help="只输出结构化 JSON。")
    args = parser.parse_args(argv)

    report = run_doctor(args.config)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print_human_report(report)
    return 1 if report.to_dict()["summary"]["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
