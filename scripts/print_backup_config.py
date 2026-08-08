"""把 [backup] 段以 JSON 输出，供 PowerShell 入口复用项目自己的 TOML 解析。

与 print_config_path.py 同源同理由：**禁止在 ps1 里手写 TOML 解析**。
路径已在 load_config 里解析成绝对路径，PowerShell 侧直接用即可。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_diary_core.config import load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="以 JSON 输出 config.toml 的 [backup] 段。")
    parser.add_argument("--config", required=True, help="config.toml 的路径。")
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser()
    if not config_path.is_file():
        print("config.toml 不存在。", file=sys.stderr)
        return 2

    try:
        backup = load_config(config_path).backup
    except Exception:
        print("读取 config.toml 失败，请检查 TOML 格式和配置项。", file=sys.stderr)
        return 2

    payload = {
        "enabled": backup.enabled,
        "bundleDest": str(backup.bundle_dest) if backup.bundle_dest else "",
        "stateFile": str(backup.state_file) if backup.state_file else "",
        "keep": backup.keep,
        "staleWarnDays": backup.stale_warn_days,
        "repos": [{"name": repo.name, "path": str(repo.path)} for repo in backup.repos],
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
