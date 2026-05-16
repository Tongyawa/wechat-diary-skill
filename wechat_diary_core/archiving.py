from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
import re

from .chat_flow import render_chat_flow
from .config import Config, load_config
from .preprocessing import ProcessedChatExport, run as preprocess_run


DATE_SUFFIX_RE = re.compile(r"_(\d{8})(?:-\d{8})?$")


def archive(
    processed_exports: Iterable[ProcessedChatExport] | str | Path,
    config: Config | None = None,
    output_root: str | Path | None = None,
) -> list[Path]:
    cfg = config or load_config()
    exports = preprocess_run(processed_exports, config=cfg) if isinstance(processed_exports, (str, Path)) else list(processed_exports)
    root = Path(output_root) if output_root is not None else cfg.paths.processed
    written: list[Path] = []

    for export in exports:
        session_dir = strip_date_suffix(export.source_folder)
        for day, messages in _group_messages_by_day(export.data.get("messages") or []).items():
            out_path = root / session_dir / f"{day}.md"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(render_chat_flow(messages), encoding="utf-8")
            written.append(out_path)

    return written


def strip_date_suffix(folder_name: str) -> str:
    return DATE_SUFFIX_RE.sub("", folder_name)


def _group_messages_by_day(messages: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for message in messages:
        grouped[_message_day(message)].append(message)
    return dict(sorted(grouped.items()))


def _message_day(message: dict[str, Any]) -> str:
    formatted = str(message.get("formattedTime") or "")
    if re.match(r"\d{4}-\d{2}-\d{2}", formatted):
        return formatted[:10]

    timestamp = int(message.get("createTime") or 0)
    return datetime.fromtimestamp(timestamp).date().isoformat()
