from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import json
import shutil

from ..config import Config, load_config
from .exceptions import InvalidExportError


Post = dict[str, Any]
TIME_PATTERN = "%Y/%m/%d %H:%M:%S"


@dataclass(frozen=True)
class MomentsExport:
    source_path: Path
    posts: list[Post]
    filters: dict[str, Any]


def load_moments_export(path: str | Path) -> MomentsExport:
    """Parse a WeFlow Moments JSON export.

    Shape (from WeFlow ``朋友圈导出_*.json``):
      ``{exportTime, totalPosts, filters: {usernames, keyword}, posts: [...]}``

    Each post has ``id, username, nickname, createTime, createTimeStr,
    contentDesc, type, media, likes, comments, location``.
    """
    file = Path(path)
    with file.open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "posts" not in data:
        raise InvalidExportError(f"Not a Moments export: {file}")
    posts = list(data.get("posts") or [])
    filters = dict(data.get("filters") or {})
    return MomentsExport(source_path=file, posts=posts, filters=filters)


def discover_moments_exports(root: str | Path) -> list[Path]:
    """Find Moments-export JSON files (depth-1 inside ``root``)."""
    path = Path(root)
    if path.is_file() and _looks_like_moments_export(path):
        return [path]
    if not path.exists():
        return []
    found: list[Path] = []
    for candidate in sorted(path.rglob("*.json")):
        if _looks_like_moments_export(candidate):
            found.append(candidate)
    return found


def render_moments_flow(posts: Sequence[Post]) -> str:
    """Render a chronologically-ordered list of posts as a single markdown block.

    Per-post format::

        2026-05-14 12:22:55
        Alice：朋友吃八点钟咖啡
        [图片：media/...jpg]
        ❤ Bob (1)
        💬 Carol：好看
        💬 Alice 回复 Carol：是的
        📍 武汉 - 武汉大学

    Posts are separated by blank lines. Image / video paths come from
    ``media[].localPath`` verbatim — Agent reads the bytes multimodally later.
    """
    sorted_posts = sorted(posts, key=lambda post: int(post.get("createTime") or 0))
    blocks: list[str] = []
    for post in sorted_posts:
        block = _render_single_post(post)
        if block:
            blocks.append(block)
    return ("\n\n".join(blocks).rstrip() + "\n") if blocks else ""


def archive_moments_for(
    usernames: Sequence[str] | None = None,
    raw_path: str | Path | None = None,
    config: Config | None = None,
    subroot: str | Path = "_targets/moments",
    clear_first: bool = True,
) -> list[Path]:
    """Archive Moments exports into ``<paths.processed>/<subroot>/<yyyy-mm-dd>.md``.

    If ``usernames`` is given, only posts whose ``username`` matches are kept;
    otherwise all posts in every discovered export are archived. Private
    per-contact callers typically pass ``usernames=[target_wxid]`` and a
    matching ``subroot``.
    """
    cfg = config or load_config()
    source = Path(raw_path) if raw_path is not None else cfg.paths.raw
    target_set: set[str] | None = (
        {str(name).strip() for name in usernames if str(name).strip()}
        if usernames is not None
        else None
    )

    root = cfg.paths.processed / Path(subroot)
    if clear_first and root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    all_posts: list[Post] = []
    for export_path in discover_moments_exports(source):
        export = load_moments_export(export_path)
        for post in export.posts:
            if target_set is not None and str(post.get("username") or "").strip() not in target_set:
                continue
            all_posts.append(post)

    by_day = _group_posts_by_day(all_posts)
    written: list[Path] = []
    for day, posts in sorted(by_day.items()):
        out_path = root / f"{day}.md"
        out_path.write_text(render_moments_flow(posts), encoding="utf-8")
        written.append(out_path)
    return written


def _render_single_post(post: Post) -> str:
    timestamp = _format_time(post)
    nickname = _safe(post.get("nickname")) or _safe(post.get("username")) or "未知"
    content = _compact_text(_safe(post.get("contentDesc")))

    lines: list[str] = [timestamp, f"{nickname}：{content}" if content else f"{nickname}："]

    for media in post.get("media") or []:
        local_path = _safe(media.get("localPath"))
        if not local_path:
            continue
        suffix = Path(local_path).suffix.lower()
        if suffix in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
            lines.append(f"[视频：{local_path}]")
        else:
            lines.append(f"[图片：{local_path}]")

    likes = [_safe(name) for name in post.get("likes") or []]
    likes = [name for name in likes if name]
    if likes:
        if len(likes) <= 5:
            lines.append(f"❤ {'、'.join(likes)} ({len(likes)})")
        else:
            preview = "、".join(likes[:5])
            lines.append(f"❤ {preview}… ({len(likes)})")

    for comment in post.get("comments") or []:
        speaker = _safe(comment.get("nickname")) or "未知"
        target = _safe(comment.get("refNickname"))
        body = _compact_text(_safe(comment.get("content")))
        if target:
            lines.append(f"💬 {speaker} 回复 {target}：{body}")
        else:
            lines.append(f"💬 {speaker}：{body}")

    location = post.get("location") or {}
    poi = _safe(location.get("poiName")) or _safe(location.get("address"))
    city = _safe(location.get("cityName"))
    pieces = [piece for piece in (city, poi) if piece]
    if pieces:
        lines.append(f"📍 {' - '.join(pieces)}")

    return "\n".join(lines)


def _group_posts_by_day(posts: Iterable[Post]) -> dict[str, list[Post]]:
    grouped: dict[str, list[Post]] = defaultdict(list)
    for post in posts:
        grouped[_post_day(post)].append(post)
    return dict(grouped)


def _post_day(post: Post) -> str:
    formatted = _safe(post.get("createTimeStr"))
    if formatted:
        try:
            return datetime.strptime(formatted, TIME_PATTERN).date().isoformat()
        except ValueError:
            pass
    timestamp = int(post.get("createTime") or 0)
    return datetime.fromtimestamp(timestamp).date().isoformat() if timestamp else "unknown"


def _format_time(post: Post) -> str:
    formatted = _safe(post.get("createTimeStr"))
    if formatted:
        try:
            return datetime.strptime(formatted, TIME_PATTERN).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return formatted
    timestamp = int(post.get("createTime") or 0)
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S") if timestamp else "未知时间"


def _compact_text(value: str) -> str:
    lines = [line.strip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line]
    return " | ".join(lines)


def _safe(value: Any) -> str:
    return str(value or "").strip()


def _looks_like_moments_export(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False
    if not (isinstance(data, dict) and isinstance(data.get("posts"), list)):
        return False
    # WeFlow moments exports also carry one of these markers — guards against
    # chat exports that happen to include a "posts" key.
    return any(key in data for key in ("totalPosts", "filters", "exportTime"))
