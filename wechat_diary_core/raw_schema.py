from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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
    if not isinstance(session, dict):
        issues.append("session (object)")
    else:
        _require_fields(session, "session", _CHAT_SESSION_REQUIRED, issues)

    if not isinstance(messages, list):
        issues.append("messages (array)")
    else:
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
