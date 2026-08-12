from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


WORKSPACE_ENV_VAR = "WECHAT_DIARY_WORKSPACE"


class WorkspaceResolutionError(FileNotFoundError):
    """面向用户的入口无法按约定找到 config.toml。"""

    def __init__(self, probes: list[tuple[str, Path]], *, explicit_option: str) -> None:
        self.probes = tuple((label, path) for label, path in probes)
        self.explicit_option = explicit_option
        super().__init__(_format_error(self.probes, explicit_option=explicit_option))


def resolve_config_path(
    explicit_config: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    explicit_option: str = "--config",
    allow_missing_explicit_config: bool = False,
) -> Path:
    """按“显式配置 → CWD → 环境变量”发现 config.toml。

    显式目标一旦给出就是唯一目标，不存在时默认硬失败。只有具备首启
    创建职责的 daily 入口可显式启用 ``allow_missing_explicit_config``。
    """

    current_dir = _absolute_path(cwd if cwd is not None else Path.cwd(), base=Path.cwd())
    environment = os.environ if environ is None else environ
    probes: list[tuple[str, Path]] = []

    if explicit_config is not None and str(explicit_config).strip():
        explicit_path = _absolute_path(explicit_config, base=current_dir)
        probes.append((f"显式 {explicit_option}", explicit_path))
        if explicit_path.is_file() or allow_missing_explicit_config:
            return explicit_path
        raise WorkspaceResolutionError(probes, explicit_option=explicit_option)

    cwd_config = (current_dir / "config.toml").resolve()
    probes.append(("当前目录", cwd_config))
    if cwd_config.is_file():
        return cwd_config

    workspace_value = environment.get(WORKSPACE_ENV_VAR, "").strip()
    if workspace_value:
        workspace = _absolute_path(workspace_value, base=current_dir)
        env_config = (workspace / "config.toml").resolve()
        probes.append((f"环境变量 {WORKSPACE_ENV_VAR}", env_config))
        if env_config.is_file():
            return env_config

    raise WorkspaceResolutionError(probes, explicit_option=explicit_option)


def _absolute_path(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _format_error(probes: tuple[tuple[str, Path], ...], *, explicit_option: str) -> str:
    lines = ["找不到 config.toml。已按顺序探测以下绝对路径："]
    lines.extend(f"  - {label}：{path}" for label, path in probes)
    lines.extend(
        (
            "下一步任选一种：",
            '  1. cd "<工作区目录>" 后重试；',
            f'  2. 传 {explicit_option} "<config.toml 的绝对路径>"；',
            f'  3. 设置环境变量 {WORKSPACE_ENV_VAR} 为工作区目录后重试。',
        )
    )
    return "\n".join(lines)
