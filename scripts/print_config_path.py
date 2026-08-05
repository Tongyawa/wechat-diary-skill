"""读取配置中的路径字段，供 PowerShell 入口复用项目自己的 TOML 解析逻辑。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_diary_core.config import load_config  # noqa: E402


def _get_config_value(config: object, key: str) -> object:
    """按点号路径读取配置对象的属性。"""

    current = config
    for part in key.split("."):
        if not part or not hasattr(current, part):
            raise ValueError(f"配置中不存在路径：{key}")
        current = getattr(current, part)
    return current


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="读取 config.toml 中的配置路径。")
    parser.add_argument("--config", required=True, help="config.toml 的路径。")
    parser.add_argument("--key", required=True, help="配置属性路径，例如 paths.insights。")
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser()
    if not config_path.is_file():
        print("config.toml 不存在。", file=sys.stderr)
        return 2

    try:
        config = load_config(config_path)
        value = _get_config_value(config, args.key)
        if not isinstance(value, (str, Path)):
            raise ValueError(f"配置路径不是字符串：{args.key}")
    except Exception:
        print("读取 config.toml 失败，请检查 TOML 格式和配置项。", file=sys.stderr)
        return 2

    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
