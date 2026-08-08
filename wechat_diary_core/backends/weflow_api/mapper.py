"""Map WeFlow HTTP API responses into canonical raw schema v1."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ...raw_schema import validate_moments_json, validate_session_json
from ..weflow.naming import sanitize_session_name
from .appmsg import AppmsgMeta, parse_appmsg
from .type_map import resolve_message_type, unpack_local_type


def session_date_suffix(start: date, end: date) -> str:
    first = start.strftime("%Y%m%d")
    last = end.strftime("%Y%m%d")
    return first if first == last else f"{first}-{last}"


def session_directory_name(session: Mapping[str, Any], start: date, end: date) -> str:
    username = str(session.get("username") or "")
    prefix = "群聊" if username.endswith("@chatroom") else "私聊"
    display_name = sanitize_session_name(session.get("displayName"), fallback=username or "未命名会话")
    return f"{prefix}_{display_name}_{session_date_suffix(start, end)}"


def map_session_json(
    session: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    *,
    contacts: Sequence[Mapping[str, Any]],
    group_members: Sequence[Mapping[str, Any]] = (),
    self_wxids: Sequence[str] = (),
    media_dir: Path | None = None,
    transcriber: Any = None,
    asr_unavailable_reason: str = "ASR未启用",
    emit_emotion: bool = True,
    require_media: bool = True,
    appmsg_text_max_chars: int = 300,
) -> dict[str, Any]:
    """Map one talker; callable independently from the daily runner."""

    if len(contacts) == 100:
        raise ValueError("联系人名册恰好 100 条，疑似被 /contacts 默认 limit 截断")
    talker = str(session.get("username") or "")
    if not talker:
        raise ValueError("session.username 不能为空")
    is_group = talker.endswith("@chatroom")
    contact_index = _roster_index(contacts, "username")
    member_index = _roster_index(group_members, "wxid")
    session_contact = contact_index.get(talker, {})
    display_name = str(session.get("displayName") or _display_name(session_contact, talker))
    nickname = str(session_contact.get("nickname") or display_name)
    remark = str(session_contact.get("remark") or "")
    ordered_messages = sorted(
        messages,
        key=lambda message: (
            _message_sort_value(message.get("createTime")),
            _message_sort_value(message.get("localId")),
        ),
    )
    observed_self_wxids = [
        str(message.get("senderUsername"))
        for message in ordered_messages
        if bool(message.get("isSend")) and str(message.get("senderUsername") or "")
    ]
    effective_self_wxids = list(dict.fromkeys([*observed_self_wxids, *map(str, self_wxids)]))
    mapped_messages = [
        _map_message(
            message,
            talker=talker,
            is_group=is_group,
            session_display_name=display_name,
            contacts=contact_index,
            members=member_index,
            self_wxids=effective_self_wxids,
            media_dir=media_dir,
            transcriber=transcriber,
            asr_unavailable_reason=asr_unavailable_reason,
            emit_emotion=emit_emotion,
            require_media=require_media,
            appmsg_text_max_chars=appmsg_text_max_chars,
        )
        for message in ordered_messages
    ]
    data = {
        "session": {
            "wxid": talker,
            "username": talker,
            "displayName": display_name,
            "nickname": nickname,
            "remark": remark,
            "type": "群聊" if is_group else "私聊",
            "messageCount": len(mapped_messages),
        },
        "messages": mapped_messages,
        "weflow": {"source": "http_api", "format": "json"},
    }
    validate_session_json(data)
    return data


def _message_sort_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def write_session_export(
    raw_root: Path,
    session: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    *,
    start: date,
    end: date,
    contacts: Sequence[Mapping[str, Any]],
    group_members: Sequence[Mapping[str, Any]] = (),
    self_wxids: Sequence[str] = (),
    transcriber: Any = None,
    asr_unavailable_reason: str = "ASR未启用",
    emit_emotion: bool = True,
    require_media: bool = True,
    appmsg_text_max_chars: int = 300,
) -> Path:
    """Validate and atomically publish one complete canonical session directory."""

    raw_root.mkdir(parents=True, exist_ok=True)
    staging_parent = raw_root.parent / f".{raw_root.name}.weflow-api-staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    directory_name = session_directory_name(session, start, end)
    staging_dir = Path(tempfile.mkdtemp(prefix=f"{directory_name}-", dir=staging_parent))
    try:
        media_dir = staging_dir / "media"
        data = map_session_json(
            session,
            messages,
            contacts=contacts,
            group_members=group_members,
            self_wxids=self_wxids,
            media_dir=media_dir,
            transcriber=transcriber,
            asr_unavailable_reason=asr_unavailable_reason,
            emit_emotion=emit_emotion,
            require_media=require_media,
            appmsg_text_max_chars=appmsg_text_max_chars,
        )
        json_path = staging_dir / f"{directory_name}.json"
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        validate_session_json(json.loads(json_path.read_text(encoding="utf-8")))
        destination = raw_root / directory_name
        _replace_directory(staging_dir, destination)
        return destination
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        try:
            staging_parent.rmdir()
        except OSError:
            pass


@dataclass(frozen=True)
class MomentsExportResult:
    path: Path
    missing_media_posts: int
    published_media: int


def map_moments_json(
    exported: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    usernames: Sequence[str],
    start: date,
    end: date,
    media_dir: Path | None = None,
) -> dict[str, Any]:
    data, _missing_media_posts = _map_moments_with_stats(
        exported,
        usernames=usernames,
        start=start,
        end=end,
        media_dir=media_dir,
    )
    return data


def _map_moments_with_stats(
    exported: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    usernames: Sequence[str],
    start: date,
    end: date,
    media_dir: Path | None,
) -> tuple[dict[str, Any], int]:
    targets = {str(value) for value in usernames if str(value)}
    if isinstance(exported, Mapping):
        source_posts = exported.get("posts")
        if not isinstance(source_posts, list) or any(not isinstance(item, Mapping) for item in source_posts):
            raise ValueError("朋友圈导出 JSON 缺少 posts[]")
        posts: Iterable[Mapping[str, Any]] = source_posts
        source_export_time = exported.get("exportTime")
    else:
        posts = exported
        source_export_time = None
    mapped: list[dict[str, Any]] = []
    missing_media_posts = 0
    for post in posts:
        username = str(post.get("username") or "")
        timestamp = int(post.get("createTime") or 0)
        # Defense in depth: /sns/export honors the range, but canonical output
        # still enforces both the requested identities and local calendar days.
        local_day = datetime.fromtimestamp(timestamp).date() if timestamp else None
        if username not in targets or local_day is None or not (start <= local_day <= end):
            continue
        location = post.get("location") if isinstance(post.get("location"), Mapping) else {}
        media = post.get("media") if isinstance(post.get("media"), list) else []
        comments = post.get("comments") if isinstance(post.get("comments"), list) else []
        post_id = str(post.get("id") or post.get("tid") or "")
        mapped_media, media_missing = _map_moment_media_list(post_id, media, media_dir)
        if media_missing:
            missing_media_posts += 1
        mapped.append(
            {
                "id": post_id,
                "username": username,
                "nickname": str(post.get("nickname") or username),
                "createTime": timestamp,
                "createTimeStr": str(post.get("createTimeStr") or datetime.fromtimestamp(timestamp).strftime("%Y/%m/%d %H:%M:%S")),
                "contentDesc": str(post.get("contentDesc") or ""),
                "type": post.get("type", ""),
                "media": mapped_media,
                "likes": post.get("likes") if isinstance(post.get("likes"), list) else [],
                "comments": [_map_comment(item) for item in comments if isinstance(item, Mapping)],
                "location": {
                    "poiName": location.get("poiName", ""),
                    "address": location.get("address", ""),
                    "cityName": location.get("cityName", ""),
                },
            }
        )
    data = {
        "filters": {"usernames": sorted(targets)},
        "posts": mapped,
        "totalPosts": len(mapped),
        "exportTime": str(source_export_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    }
    validate_moments_json(data)
    return data, missing_media_posts


def moments_filename(usernames: Sequence[str], export_date: date) -> str:
    joined = "".join(sorted(str(value) for value in usernames if str(value)))
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:8]
    return f"朋友圈导出_{export_date:%Y%m%d}_{digest}.json"


def write_moments_export(
    raw_root: Path,
    exported_json: Mapping[str, Any] | Path,
    *,
    usernames: Sequence[str],
    export_date: date,
    staging_dir: Path,
) -> MomentsExportResult:
    """Publish staged WeFlow moments media first and canonical JSON last."""

    raw_root.mkdir(parents=True, exist_ok=True)
    staging_dir = staging_dir.resolve()
    if isinstance(exported_json, Path):
        source_path = exported_json.resolve()
        _require_inside(source_path, staging_dir, "WeFlow 朋友圈 JSON")
        source_data = json.loads(source_path.read_text(encoding="utf-8"))
    else:
        source_path = None
        source_data = exported_json
    if not isinstance(source_data, Mapping):
        raise ValueError("朋友圈导出 JSON 顶层必须是 object")

    media_source = staging_dir / "media"
    data, missing_media_posts = _map_moments_with_stats(
        source_data,
        usernames=usernames,
        start=export_date,
        end=export_date,
        media_dir=media_source,
    )
    destination = raw_root / moments_filename(usernames, export_date)
    canonical_stage = staging_dir / destination.name
    temp_path = staging_dir / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    validate_moments_json(json.loads(temp_path.read_text(encoding="utf-8")))
    os.replace(temp_path, canonical_stage)
    if source_path is not None and source_path != canonical_stage:
        source_path.unlink(missing_ok=True)

    published_media = 0
    selected_media_prefixes = {
        f"{post['id']}_" for post in data["posts"] if str(post.get("id") or "")
    }
    if media_source.is_dir():
        for source_media in sorted(path for path in media_source.rglob("*") if path.is_file()):
            if not any(source_media.name.startswith(prefix) for prefix in selected_media_prefixes):
                continue
            relative = source_media.relative_to(media_source)
            destination_media = raw_root / "media" / relative
            destination_media.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source_media, destination_media)
            published_media += 1

    for post in data["posts"]:
        for media in post["media"]:
            relative_text = str(media.get("localPath") or "")
            if relative_text and not (raw_root / Path(relative_text)).is_file():
                raise FileNotFoundError(f"朋友圈媒体发布后不存在：{relative_text}")

    # JSON is the live discovery marker, so it is published only after every
    # available media file is in place. Missing decrypted media stays URL-only.
    os.replace(canonical_stage, destination)
    return MomentsExportResult(
        path=destination,
        missing_media_posts=missing_media_posts,
        published_media=published_media,
    )


def _map_message(
    message: Mapping[str, Any],
    *,
    talker: str,
    is_group: bool,
    session_display_name: str,
    contacts: Mapping[str, Mapping[str, Any]],
    members: Mapping[str, Mapping[str, Any]],
    self_wxids: Sequence[str],
    media_dir: Path | None,
    transcriber: Any,
    asr_unavailable_reason: str,
    emit_emotion: bool,
    require_media: bool,
    appmsg_text_max_chars: int,
) -> dict[str, Any]:
    local_type = int(message.get("localType") or 0)
    base, app_type = unpack_local_type(local_type)
    resolved = resolve_message_type(local_type)
    is_send = int(bool(message.get("isSend")))
    sender = str(message.get("senderUsername") or "")
    if not sender:
        if is_group:
            sender = talker
        elif is_send:
            sender = next((str(value) for value in self_wxids if str(value)), "self")
        else:
            sender = talker

    roster = members if is_group else contacts
    sender_record = roster.get(sender) or contacts.get(sender) or {}
    if sender == talker and not sender_record:
        sender_display = session_display_name
    else:
        sender_display = _display_name(sender_record, sender)

    relative_media = _localize_media(message, base, media_dir, require_media=require_media)
    content = _message_content(
        message,
        resolved.placeholder,
        base,
        app_type,
        relative_media,
        appmsg_text_max_chars,
    )
    emotion: dict[str, list[str]] | None = None
    if base == 34:
        content, emotion = _voice_content(
            relative_media,
            media_dir=media_dir,
            transcriber=transcriber,
            unavailable_reason=asr_unavailable_reason,
        )

    timestamp = int(message.get("createTime") or 0)
    mapped: dict[str, Any] = {
        "localId": message.get("localId", ""),
        "createTime": timestamp,
        "formattedTime": datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S"),
        "type": resolved.canonical,
        "content": content,
        "source": relative_media or "",
        "isSend": is_send,
        "senderUsername": sender,
        "senderDisplayName": sender_display,
        "senderRemark": str(sender_record.get("remark") or ""),
        "senderNickname": str(
            sender_record.get("nickname") or sender_record.get("groupNickname") or ""
        ),
    }
    server_id = message.get("serverId")
    if server_id not in (None, ""):
        mapped["platformMessageId"] = str(server_id)
    if emit_emotion and emotion and (emotion["emotion"] or emotion["events"]):
        mapped["voiceEmotion"] = emotion

    quote = message.get("quote") if isinstance(message.get("quote"), Mapping) else {}
    reply_id = message.get("replyToMessageId")
    if reply_id not in (None, "") or quote:
        if reply_id not in (None, ""):
            mapped["replyToMessageId"] = str(reply_id)
        elif quote.get("platformMessageId") not in (None, ""):
            mapped["replyToMessageId"] = str(quote["platformMessageId"])
        quoted_sender = str(quote.get("sender") or "")
        mapped["quotedContent"] = str(quote.get("content") or "")
        mapped["quotedSender"] = quoted_sender
        mapped["quotedSenderDisplayName"] = str(
            quote.get("accountName")
            or _display_name(members.get(quoted_sender) or contacts.get(quoted_sender) or {}, quoted_sender)
        )
    return mapped


def _message_content(
    message: Mapping[str, Any],
    placeholder: str,
    base: int,
    app_type: int,
    relative_media: str,
    appmsg_text_max_chars: int = 300,
) -> str:
    if base in {3, 47}:
        return relative_media or placeholder
    if base in {1, 50, 10000}:
        return str(message.get("content") or message.get("rawContent") or placeholder)
    if base != 49:
        return placeholder or str(message.get("content") or "")
    content = str(message.get("content") or "")
    if app_type == 57:
        # 57 的 content 是回复正文纯文本、不是 XML；真机实测 title 命中率 0/4341。
        return content or placeholder
    meta = parse_appmsg(content, max_chars=appmsg_text_max_chars)
    if meta is None:
        return placeholder or content
    return _render_appmsg(meta, app_type, placeholder)


def _with_body(label: str, body: str) -> str:
    return f"[{label}] {body}" if body else f"[{label}]"


def _with_title_des(label: str, title: str, des: str, separator: str = "：") -> str:
    if title and des:
        return f"[{label}] {title}{separator}{des}"
    return _with_body(label, title or des)


MAX_FORMATTABLE_FILE_SIZE = (1 << 63) - 1


def _format_file_size(total: int) -> str | None:
    if total < 0 or total > MAX_FORMATTABLE_FILE_SIZE:
        return None
    if total < 1024:
        return f"{total} B"
    value = float(total)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"


def _render_appmsg(meta: AppmsgMeta, app_type: int, placeholder: str) -> str:
    if app_type in {8, 47}:
        return placeholder
    if app_type in {6, 74}:
        filename = re.split(r"[\\/]", meta.title)[-1] if meta.title else ""
        if not filename:
            return "[文件]"
        formatted_size = _format_file_size(meta.totallen) if meta.totallen is not None else None
        if formatted_size is None:
            return f"[文件：{filename}]"
        return f"[文件：{filename}（{formatted_size}）]"
    if app_type == 62:
        return meta.title or placeholder
    if app_type == 63:
        return _with_body("视频号", meta.title)
    if app_type == 53:
        return _with_body("接龙", meta.title)
    if app_type == 5:
        return _with_title_des("链接", meta.title, meta.des)
    if app_type == 51:
        return _with_body("动态", meta.title)
    if app_type == 33:
        return _with_body("小程序", meta.title)
    if app_type == 19:
        return _with_title_des("合并转发", meta.title, meta.des)
    if app_type == 4:
        return _with_title_des("视频分享", meta.title, meta.des)
    if app_type == 2000:
        return _with_body("转账", meta.des)
    if app_type == 2001:
        return _with_body("红包", meta.des)
    if app_type == 87:
        return "[群公告]"
    if app_type == 24:
        return _with_body("收藏", meta.des)
    if app_type == 1:
        return _with_body("链接", meta.title)
    if app_type == 36:
        return _with_body("分享", meta.title)
    if app_type == 3:
        if meta.title and meta.des:
            return f"[音乐] {meta.title} - {meta.des}"
        return _with_body("音乐", meta.title or meta.des)
    if app_type == 50:
        return _with_body("视频号", meta.title)
    if meta.title:
        return f"[其他消息] {meta.title}"
    return placeholder


def _voice_content(
    relative_media: str,
    *,
    media_dir: Path | None,
    transcriber: Any,
    unavailable_reason: str,
) -> tuple[str, dict[str, list[str]] | None]:
    if not relative_media or media_dir is None:
        return "[语音消息 - 转文字失败: 媒体缺失]", None
    audio_path = media_dir.parent / Path(relative_media)
    if transcriber is None:
        return f"[语音消息 - 转文字失败: {unavailable_reason}]", None
    try:
        if hasattr(transcriber, "transcribe"):
            result = transcriber.transcribe(audio_path)
        elif isinstance(transcriber, Callable):
            result = transcriber(audio_path)
        else:
            raise TypeError("ASR 对象不可调用")
        text = str(result.get("text") or "").strip()
        if not text:
            raise RuntimeError("无可用文字")
        emotion = {
            "emotion": [str(value) for value in result.get("emotion") or []],
            "events": [str(value) for value in result.get("events") or []],
        }
        return f"[语音转文字] {text}", emotion
    except Exception:
        # A single bad voice never aborts its session export.
        return "[语音消息 - 转文字失败: SenseVoice转写失败]", None


def _localize_media(
    message: Mapping[str, Any],
    base: int,
    media_dir: Path | None,
    *,
    require_media: bool,
) -> str:
    if base not in {3, 34, 47} or media_dir is None:
        return ""
    source_text = str(message.get("mediaLocalPath") or "")
    if not source_text:
        if base == 3 and require_media:
            raise FileNotFoundError("图片消息缺少 mediaLocalPath")
        return ""
    source = Path(source_text)
    if not source.is_file():
        if base == 3:
            raise FileNotFoundError(f"图片媒体不存在：{source}")
        return ""
    subdir = {3: "images", 34: "voices", 47: "emojis"}[base]
    filename = sanitize_session_name(
        message.get("mediaFileName") or source.name,
        fallback=f"media_{message.get('localId') or uuid.uuid4().hex}",
    )
    destination_dir = media_dir / subdir
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / filename
    if destination.exists() and destination.resolve() != source.resolve():
        destination = destination_dir / f"{message.get('localId') or uuid.uuid4().hex}_{filename}"
    shutil.copy2(source, destination)
    return destination.relative_to(media_dir.parent).as_posix()


def _roster_index(
    values: Sequence[Mapping[str, Any]],
    key: str,
) -> dict[str, Mapping[str, Any]]:
    return {str(item.get(key)): item for item in values if str(item.get(key) or "")}


def _display_name(record: Mapping[str, Any], fallback: str) -> str:
    return str(
        record.get("displayName")
        or record.get("groupNickname")
        or record.get("nickname")
        or record.get("remark")
        or fallback
        or "未知"
    )


def _map_moment_media_list(
    post_id: str,
    media: Sequence[Any],
    media_dir: Path | None,
) -> tuple[list[dict[str, Any]], bool]:
    items = [item for item in media if isinstance(item, Mapping)]
    candidates: list[Path] = []
    if media_dir is not None and media_dir.is_dir() and post_id:
        prefix = f"{post_id}_"
        candidates = sorted(
            (
                path
                for path in media_dir.rglob("*")
                if path.is_file()
                and path.name.startswith(prefix)
                and not path.stem.endswith("_live")
            ),
            key=_moment_media_sort_key,
        )

    mapped_items: list[dict[str, Any]] = []
    missing = False
    for index, item in enumerate(items):
        mapped = dict(item)
        mapped.pop("mediaLocalPath", None)
        mapped.pop("localPath", None)
        if index < len(candidates) and media_dir is not None:
            relative = candidates[index].relative_to(media_dir)
            mapped["localPath"] = (Path("media") / relative).as_posix()
        else:
            # Preserve url/thumb and all other metadata. URL-only is the
            # canonical degradation for a post whose media was not decrypted.
            missing = True
        mapped_items.append(mapped)
    return mapped_items, missing


def _moment_media_sort_key(path: Path) -> tuple[int, str]:
    tail = path.stem.rsplit("_", 1)[-1]
    return (int(tail) if tail.isdigit() else 10**9, path.name)


def _map_comment(item: Mapping[str, Any]) -> dict[str, Any]:
    mapped = dict(item)
    mapped.setdefault("nickname", "")
    mapped.setdefault("content", "")
    mapped.setdefault("refNickname", "")
    return mapped


def _require_inside(path: Path, parent: Path, label: str) -> None:
    try:
        path.relative_to(parent)
    except ValueError as exc:
        raise ValueError(f"{label} 不在 staging 目录内：{path}") from exc


def _replace_directory(source: Path, destination: Path) -> None:
    backup: Path | None = None
    if destination.exists():
        # Keep the old complete snapshot outside live raw while swapping; a
        # crash can leave a recoverable backup, never a discoverable half-run.
        backup = source.parent / f".{destination.name}.old-{uuid.uuid4().hex}"
        os.replace(destination, backup)
    try:
        os.replace(source, destination)
    except Exception:
        if backup is not None and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    if backup is not None:
        shutil.rmtree(backup)


__all__ = [
    "MomentsExportResult",
    "map_moments_json",
    "map_session_json",
    "moments_filename",
    "session_date_suffix",
    "session_directory_name",
    "write_moments_export",
    "write_session_export",
]
