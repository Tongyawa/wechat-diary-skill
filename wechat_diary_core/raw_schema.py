from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .media_store import MEDIA_SCHEME_KEY, MEDIA_SCHEME_VALUE


SCHEMA_VERSION = 1


class RawSchemaError(ValueError):
    """Raised when a raw export is missing canonical schema v1 requirements."""


_CHAT_SESSION_REQUIRED: dict[str, tuple[type, ...]] = {
    "wxid": (str,),
    "nickname": (str,),
    "remark": (str,),
    "displayName": (str,),
    "type": (str,),
}

_CHAT_MESSAGE_REQUIRED: dict[str, tuple[type, ...]] = {
    "localId": (int, str),
    "createTime": (int, str),
    "formattedTime": (str,),
    "type": (str,),
    "source": (str,),
    "isSend": (int, bool, str),
    "senderUsername": (str,),
    "senderDisplayName": (str,),
}

_CHAT_CONTENT_GROUP_LABELS = {
    "text": "文本消息",
    "reply": "引用消息正文",
    "image": "图片消息",
    "voice": "语音消息",
    "video": "视频消息",
    "other": "其他内容型消息",
}

_CAS_CHAT_REFERENCE_RE = re.compile(
    r"^\.\./_media/(?P<class>images|voices|emojis|videos)/"
    r"(?P<shard>[0-9a-f]{2})/(?P<md5>[0-9a-f]{32})\.(?P<ext>[a-z0-9]{1,16})$"
)
_CAS_ROOT_REFERENCE_RE = re.compile(
    r"^_media/(?P<class>images|voices|emojis|videos)/"
    r"(?P<shard>[0-9a-f]{2})/(?P<md5>[0-9a-f]{32})\.(?P<ext>[a-z0-9]{1,16})$"
)

_MOMENTS_FILTER_REQUIRED: dict[str, tuple[type, ...]] = {
    "usernames": (list,),
}

_MOMENTS_POST_REQUIRED: dict[str, tuple[type, ...]] = {
    "username": (str,),
    "nickname": (str,),
    "createTime": (int, str),
    "createTimeStr": (str,),
    "contentDesc": (str,),
    "media": (list,),
    "comments": (list,),
    "location": (dict,),
}


def validate_session_json(data: Any) -> None:
    """Validate the required subset of canonical raw chat schema v1."""
    issues: list[str] = []
    if not isinstance(data, dict):
        raise RawSchemaError("canonical raw chat schema v1 validation failed: <root> must be object")

    session = data.get("session")
    messages = data.get("messages")
    media_scheme = _validate_media_scheme(data.get("weflow"), issues)
    if not isinstance(session, dict):
        issues.append("session (object)")
    else:
        _require_fields(session, "session", _CHAT_SESSION_REQUIRED, issues)

    if not isinstance(messages, list):
        issues.append("messages (array)")
    else:
        content_contract_counts = {group: [0, 0] for group in _CHAT_CONTENT_GROUP_LABELS}
        previous_create_time: int | None = None
        first_order_violation: tuple[int, int, int] | None = None
        order_violation_count = 0
        for index, message in enumerate(messages):
            path = f"messages[{index}]"
            if not isinstance(message, dict):
                issues.append(f"{path} (object)")
                continue
            _require_fields(message, path, _CHAT_MESSAGE_REQUIRED, issues)
            _validate_optional_reply_context(message.get("replyContext"), path, issues)
            if media_scheme == MEDIA_SCHEME_VALUE:
                _validate_cas_message_reference(message, path, issues)
            content_group = _chat_content_group(message)
            if content_group is not None:
                counts = content_contract_counts[content_group]
                counts[0] += 1
                if _message_has_renderable_payload(message, content_group):
                    counts[1] += 1
            if "createTime" not in message:
                continue
            create_time = _message_numeric_value(message.get("createTime"))
            if previous_create_time is not None and create_time < previous_create_time:
                order_violation_count += 1
                if first_order_violation is None:
                    first_order_violation = (index, previous_create_time, create_time)
            previous_create_time = create_time
        if first_order_violation is not None:
            first_index, first_previous_time, first_current_time = first_order_violation
            issues.append(
                f"messages[{first_index}].createTime 顺序错误：canonical raw 消息必须按 createTime 单调不减 "
                f"（首个违规处 createTime {first_previous_time} → {first_current_time}，"
                f"共 {order_violation_count} 处），请重新导出"
            )
        for group, (message_count, valid_count) in content_contract_counts.items():
            if message_count and valid_count == 0:
                label = _CHAT_CONTENT_GROUP_LABELS[group]
                issues.append(
                    f"messages 内容契约：{label}共 {message_count} 条但全部缺少可渲染载荷；"
                    "请检查导出 mapper 的 content/source 映射后重新导出"
                )

    _raise_if_issues("chat", issues)


def validate_moments_json(data: Any) -> None:
    """Validate the required subset of canonical raw Moments schema v1."""
    issues: list[str] = []
    if not isinstance(data, dict):
        raise RawSchemaError("canonical raw moments schema v1 validation failed: <root> must be object")

    filters = data.get("filters")
    posts = data.get("posts")
    if not isinstance(filters, dict):
        issues.append("filters (object)")
    else:
        _require_fields(filters, "filters", _MOMENTS_FILTER_REQUIRED, issues)

    if not isinstance(posts, list):
        issues.append("posts (array)")
    else:
        for index, post in enumerate(posts):
            path = f"posts[{index}]"
            if not isinstance(post, dict):
                issues.append(f"{path} (object)")
                continue
            _require_fields(post, path, _MOMENTS_POST_REQUIRED, issues)
            _validate_moments_media(post.get("media"), path, issues)
            _validate_moments_comments(post.get("comments"), path, issues)
            _validate_moments_location(post.get("location"), path, issues)

    _raise_if_issues("moments", issues)


def _validate_optional_reply_context(value: Any, parent_path: str, issues: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        issues.append(f"{parent_path}.replyContext (object)")
        return
    for field in ("isSend", "senderDisplayName", "senderUsername", "content", "type"):
        if field in value:
            _require_type(value[field], f"{parent_path}.replyContext.{field}", _CHAT_MESSAGE_REQUIRED.get(field, (str,)), issues)


def _chat_content_group(message: Mapping[str, Any]) -> str | None:
    message_type = str(message.get("type") or "").strip()
    if message_type == "文本消息":
        return "text"
    if message_type == "引用消息":
        return "reply"
    if "图片" in message_type:
        return "image"
    if message_type == "语音消息":
        return "voice"
    if message_type == "视频消息":
        return "video"
    if message_type == "动画表情":
        # The renderer deterministically degrades this type to ``[表情]`` even
        # when the upstream export carries no textual body or local file.
        return None
    return "other"


def _message_has_renderable_payload(message: Mapping[str, Any], group: str) -> bool:
    content = str(message.get("content") or "").strip()
    source = str(message.get("source") or "").strip()
    if group in {"text", "reply"}:
        # Reply-target metadata is independently mapped context. It can make a
        # quote visible, but cannot prove that this message's own body survived
        # the content mapper.
        return bool(content)
    if group == "image":
        return bool(content or source) or any(
            message.get(field)
            for field in ("image_vision", "image_vision_inline", "image_ocr", "image_ocr_inline")
        )
    if group == "voice":
        return bool(content or source or message.get("transcribe_failed"))
    if group == "video":
        return bool(source or content == "[视频]")
    return bool(content)


def _validate_media_scheme(value: Any, issues: list[str]) -> str:
    if not isinstance(value, dict) or MEDIA_SCHEME_KEY not in value:
        return ""
    scheme = value.get(MEDIA_SCHEME_KEY)
    if not isinstance(scheme, str):
        issues.append(f"weflow.{MEDIA_SCHEME_KEY} (string)")
        return ""
    if scheme != MEDIA_SCHEME_VALUE:
        issues.append(
            f"weflow.{MEDIA_SCHEME_KEY}={scheme!r} 不受支持；当前契约为 {MEDIA_SCHEME_VALUE!r}"
        )
        return ""
    return scheme


def _validate_cas_message_reference(
    message: Mapping[str, Any],
    path: str,
    issues: list[str],
) -> None:
    message_type = str(message.get("type") or "")
    expected_class = {
        "图片消息": "images",
        "语音消息": "voices",
        "动画表情": "emojis",
        "视频消息": "videos",
    }.get(message_type)
    if expected_class is None:
        return
    source = str(message.get("source") or "")
    if not source:
        if message_type == "视频消息" and str(message.get("content") or "") != "[视频]":
            issues.append(f"{path} cas-v1 视频缺少 source 时 content 必须为 [视频]")
        return
    match = _CAS_CHAT_REFERENCE_RE.fullmatch(source)
    if match is None:
        issues.append(f"{path}.source 不是合法 cas-v1 相对引用：{source!r}")
        return
    if match.group("class") != expected_class:
        issues.append(
            f"{path}.source class={match.group('class')} 与消息类型 {message_type} 不一致"
        )
    if match.group("shard") != match.group("md5")[:2]:
        issues.append(f"{path}.source 的 md5 分片与文件名不一致")
    if message_type in {"图片消息", "动画表情", "视频消息"}:
        if str(message.get("content") or "") != source:
            issues.append(f"{path}.content 必须与 cas-v1 source 使用同一媒体引用")


def _validate_moments_media(value: Any, parent_path: str, issues: list[str]) -> None:
    if not isinstance(value, list):
        return
    for index, media in enumerate(value):
        path = f"{parent_path}.media[{index}]"
        if not isinstance(media, dict):
            issues.append(f"{path} (object)")
            continue
        if "localPath" in media:
            _require_type(media["localPath"], f"{path}.localPath", (str,), issues)
            _validate_root_cas_reference(media["localPath"], f"{path}.localPath", issues)
        live_photo = media.get("livePhoto")
        if live_photo is not None:
            if not isinstance(live_photo, dict):
                issues.append(f"{path}.livePhoto (object)")
            elif "localPath" in live_photo:
                _require_type(live_photo["localPath"], f"{path}.livePhoto.localPath", (str,), issues)
                _validate_root_cas_reference(
                    live_photo["localPath"],
                    f"{path}.livePhoto.localPath",
                    issues,
                    expected_class="videos",
                )


def _validate_root_cas_reference(
    value: Any,
    path: str,
    issues: list[str],
    *,
    expected_class: str | None = None,
) -> None:
    if not isinstance(value, str) or not value.startswith("_media/"):
        return
    match = _CAS_ROOT_REFERENCE_RE.fullmatch(value)
    if match is None:
        issues.append(f"{path} 不是合法 cas-v1 根级相对引用：{value!r}")
        return
    if match.group("shard") != match.group("md5")[:2]:
        issues.append(f"{path} 的 md5 分片与文件名不一致")
    if expected_class is not None and match.group("class") != expected_class:
        issues.append(f"{path} 必须引用 {expected_class} class")


def _validate_moments_comments(value: Any, parent_path: str, issues: list[str]) -> None:
    if not isinstance(value, list):
        return
    for index, comment in enumerate(value):
        path = f"{parent_path}.comments[{index}]"
        if not isinstance(comment, dict):
            issues.append(f"{path} (object)")
            continue
        for field in ("nickname", "content", "refNickname"):
            if field in comment:
                _require_type(comment[field], f"{path}.{field}", (str,), issues)


def _validate_moments_location(value: Any, parent_path: str, issues: list[str]) -> None:
    if not isinstance(value, dict):
        return
    for field in ("poiName", "address", "cityName"):
        if field in value:
            _require_type(value[field], f"{parent_path}.location.{field}", (str, int, float), issues)


def _require_fields(
    data: Mapping[str, Any],
    parent_path: str,
    requirements: Mapping[str, tuple[type, ...]],
    issues: list[str],
) -> None:
    for field, expected_types in requirements.items():
        path = f"{parent_path}.{field}"
        if field not in data:
            issues.append(path)
            continue
        _require_type(data[field], path, expected_types, issues)


def _require_type(value: Any, path: str, expected_types: tuple[type, ...], issues: list[str]) -> None:
    if isinstance(value, expected_types):
        return
    expected = "|".join(_type_name(item) for item in expected_types)
    issues.append(f"{path} ({expected})")


def _raise_if_issues(kind: str, issues: list[str]) -> None:
    if not issues:
        return
    unique_issues = list(dict.fromkeys(issues))
    missing_text = ", ".join(unique_issues)
    raise RawSchemaError(
        f"canonical raw {kind} schema v{SCHEMA_VERSION} validation failed; "
        f"missing or invalid required fields: {missing_text}"
    )


def _type_name(value: type) -> str:
    if value is str:
        return "string"
    if value is int:
        return "integer"
    if value is bool:
        return "boolean"
    if value is list:
        return "array"
    if value is dict:
        return "object"
    return value.__name__


def _message_numeric_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
