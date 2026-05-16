from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import Config, load_config


def read_archived_exports(day: date | str, config: Config | None = None) -> list[dict[str, Any]]:
    cfg = config or load_config()
    day_text = day.isoformat() if isinstance(day, date) else day
    exports: list[dict[str, Any]] = []
    for path in sorted(cfg.paths.processed.glob(f"*/{day_text}.md")):
        exports.append(
            {
                "session": {"displayName": path.parent.name},
                "chatFlow": path.read_text(encoding="utf-8-sig"),
                "path": str(path),
            }
        )
    return exports


def write_daily_markdown(kind: str, day: date | str, content: str, config: Config | None = None) -> Path:
    cfg = config or load_config()
    day_text = day.isoformat() if isinstance(day, date) else day
    year = day_text[:4]
    out_path = cfg.paths.insights / kind / year / f"{day_text}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return out_path


def write_summary(folder: str, content: str, config: Config | None = None, run_time: datetime | None = None) -> Path:
    cfg = config or load_config()
    timestamp = (run_time or datetime.now()).strftime("%Y%m%d-%H%M%S")
    out_path = cfg.paths.insights / "Summaries" / f"{folder}__{timestamp}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return out_path


def flatten_messages(exports: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for export in exports:
        session = export.get("session") or {}
        if "chatFlow" in export:
            messages.append(
                {
                    "sessionDisplayName": session.get("displayName"),
                    "sessionType": session.get("type"),
                    "type": "chat_flow",
                    "content": export.get("chatFlow") or "",
                    "sourcePath": export.get("path"),
                }
            )
            continue
        for message in export.get("messages") or []:
            enriched = dict(message)
            enriched["sessionDisplayName"] = session.get("displayName") or session.get("nickname") or session.get("remark")
            enriched["sessionType"] = session.get("type")
            messages.append(enriched)
    return messages
