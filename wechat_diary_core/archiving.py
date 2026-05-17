from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
import re
import shutil

from .chat_flow import render_chat_flow
from .config import Config, load_config
from .preprocessing import ImageMode, ProcessedChatExport, run as preprocess_run


DATE_SUFFIX_RE = re.compile(r"_(\d{8})(?:-\d{8})?$")


def archive(
    processed_exports: Iterable[ProcessedChatExport] | str | Path,
    config: Config | None = None,
    output_root: str | Path | None = None,
    clear_first: bool = True,
    image_mode: ImageMode = "ocr_inline",
) -> list[Path]:
    """Render preprocessed exports into ``WeFlow-processed-exports/<session>/<yyyy-mm-dd>.md``.

    ``clear_first=True`` (default) rmtree's the output root before writing so stale
    sessions from a previous run don't bleed into today's directory. The daily cron
    already wipes via ``export_all_chats(cleanup="delete")``, so this is mostly a
    safety net for manual re-renders.

    ``image_mode="preserve_paths"`` disables OCR and renders
    ``[图片：media/images/xxx.jpg]`` so a downstream Agent reads the image
    multimodally (used by private per-contact pipelines).
    """
    cfg = config or load_config()
    exports = (
        preprocess_run(processed_exports, config=cfg, image_mode=image_mode)
        if isinstance(processed_exports, (str, Path))
        else list(processed_exports)
    )
    root = Path(output_root) if output_root is not None else cfg.paths.processed
    if clear_first and root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for export in exports:
        session_dir = strip_date_suffix(export.source_folder)
        for day, messages in _group_messages_by_day(export.data.get("messages") or []).items():
            out_path = root / session_dir / f"{day}.md"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(render_chat_flow(messages), encoding="utf-8")
            written.append(out_path)

    return written


def archive_chats_for(
    usernames: Sequence[str],
    raw_path: str | Path | None = None,
    config: Config | None = None,
    subroot: str | Path = "_targets/chats",
    image_mode: ImageMode = "preserve_paths",
    clear_first: bool = True,
) -> list[Path]:
    """Archive only sessions whose participants match ``usernames`` into
    ``<paths.processed>/<subroot>/<yyyy-mm-dd>.md`` (no per-session folder).

    Used by private per-contact pipelines that prefer to feed the original
    image bytes to a multimodal Agent rather than baking OCR into the markdown.
    The ``subroot`` is the caller's responsibility — core stays topic-neutral.
    """
    cfg = config or load_config()
    source = Path(raw_path) if raw_path is not None else cfg.paths.raw
    target_set = {str(name).strip() for name in usernames if str(name).strip()}
    if not target_set:
        return []

    exports = preprocess_run(source, config=cfg, image_mode=image_mode)
    filtered = [export for export in exports if _export_matches_usernames(export, target_set)]

    root = cfg.paths.processed / Path(subroot)
    if clear_first and root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for export in filtered:
        for day, messages in _group_messages_by_day(export.data.get("messages") or []).items():
            out_path = root / f"{day}.md"
            existing = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
            chunk = render_chat_flow(messages)
            out_path.write_text(existing + chunk if existing else chunk, encoding="utf-8")
            if out_path not in written:
                written.append(out_path)
    return written


def promote_day_to_archive(
    day: str,
    config: Config | None = None,
    *,
    source_root: str | Path | None = None,
    archive_root: str | Path | None = None,
    move: bool = False,
) -> list[Path]:
    """Promote ``<source>/<session>/<day>.md`` files to
    ``<archive>/<session>/<day>.md`` for long-term per-session history.

    Each SKILL calls this after its二次加工 succeeds. ``move=True`` deletes the
    source file post-copy; default copies so other SKILLs running later in the
    same day can still read processed.
    """
    cfg = config or load_config()
    source = Path(source_root) if source_root is not None else cfg.paths.processed
    target = Path(archive_root) if archive_root is not None else cfg.paths.archived
    if not source.exists():
        return []

    promoted: list[Path] = []
    for md_path in source.rglob(f"{day}.md"):
        relative = md_path.relative_to(source)
        out_path = target / relative
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(str(md_path), str(out_path))
        else:
            shutil.copy2(str(md_path), str(out_path))
        promoted.append(out_path)
    return promoted


def _export_matches_usernames(export: ProcessedChatExport, usernames: set[str]) -> bool:
    session = export.data.get("session") or {}
    candidates = {
        str(session.get("username") or "").strip(),
        str(session.get("wxid") or "").strip(),
        str(session.get("displayName") or "").strip(),
        str(session.get("nickname") or "").strip(),
        str(session.get("remark") or "").strip(),
    }
    if any(name and name in usernames for name in candidates):
        return True
    # Fall back to scanning message senders (covers private chats where session
    # block is sparse but every isSend==0 message carries senderUsername).
    for message in export.data.get("messages") or []:
        sender = str(message.get("senderUsername") or "").strip()
        if sender and sender in usernames and int(message.get("isSend") or 0) == 0:
            return True
    return False


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
