"""Small standard-library client for the local WeFlow 5.x HTTP API."""

from __future__ import annotations

import json
import socket
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


MAX_HTTP_ERROR_BODY_BYTES = 64 * 1024
MAX_HTTP_ERROR_DETAIL_CHARS = 500


class WeflowApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


class WeflowApiClient:
    """Authenticated API access with reusable range/pagination methods."""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        timeout: float = 60.0,
        message_timeout: float = 600.0,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token.strip()
        self.timeout = timeout
        self.message_timeout = message_timeout
        self._opener = opener

    def health(self) -> dict[str, Any]:
        payload = self._request("/health", authenticated=False)
        if payload.get("status") != "ok":
            raise WeflowApiError("WeFlow API /health 未返回 status=ok")
        return payload

    def validate_token(self) -> None:
        self._request("/api/v1/sessions", {"limit": 1, "offset": 0})

    def fetch_sessions(self, *, limit: int = 2000) -> list[dict[str, Any]]:
        return self._fetch_offset_collection(
            "/api/v1/sessions",
            "sessions",
            limit=limit,
            identity_fields=("username",),
        )

    def get_message_page(
        self,
        talker: str,
        *,
        start: date | str,
        end: date | str,
        limit: int = 10000,
        offset: int = 0,
        media: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "talker": talker,
            "limit": limit,
            "offset": offset,
            "start": _api_date(start),
            "end": _api_date(end),
            "format": "json",
        }
        if media:
            params.update({"media": 1, "image": 1, "voice": 1, "emoji": 1})
        payload = self._request("/api/v1/messages", params, timeout=self.message_timeout)
        self._list_field(payload, "messages")
        return payload

    def fetch_messages(
        self,
        talker: str,
        *,
        start: date | str,
        end: date | str,
        limit: int = 10000,
        media: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch one talker and arbitrary inclusive date range to exhaustion."""

        messages: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        offset = 0
        declared_total: int | None = None
        while True:
            payload = self.get_message_page(
                talker,
                start=start,
                end=end,
                limit=limit,
                offset=offset,
                media=media,
            )
            page = self._list_field(payload, "messages")
            page_keys: set[tuple[str, str]] = set()
            for message in page:
                key = (str(message.get("serverId") or ""), str(message.get("localId") or ""))
                if key == ("", ""):
                    raise WeflowApiError(
                        "WeFlow API /messages 分页项缺少 serverId/localId；无法证明导出完整，请升级或检查 API"
                    )
                if key in page_keys or key in seen:
                    raise WeflowApiError(
                        "WeFlow API /messages 返回重复消息 ID；已停止以避免死循环或重复导出，请检查 API 分页"
                    )
                page_keys.add(key)
            seen.update(page_keys)
            messages.extend(page)

            page_total = _declared_total(payload, "/api/v1/messages")
            if page_total is not None:
                if declared_total is None:
                    declared_total = page_total
                elif declared_total != page_total:
                    raise WeflowApiError(
                        "WeFlow API /messages 的 count 在分页间变化；无法证明导出完整，请重试或检查 API"
                    )
                if len(seen) > declared_total:
                    raise WeflowApiError(
                        "WeFlow API /messages 唯一消息数超过 count；分页契约矛盾，请检查 API"
                    )

            has_more = _declared_has_more(payload, "/api/v1/messages")
            if has_more is False:
                if declared_total is not None and len(seen) != declared_total:
                    raise WeflowApiError(
                        f"WeFlow API /messages 提前结束：唯一消息 {len(seen)} 条，count={declared_total}；"
                        "请重试或检查 API 分页"
                    )
                return messages
            if has_more is True and not page:
                raise WeflowApiError(
                    "WeFlow API /messages 返回空页但 hasMore=true；已停止以避免死循环，请检查 API 分页"
                )
            if has_more is True and declared_total is not None and len(seen) >= declared_total:
                raise WeflowApiError(
                    "WeFlow API /messages 已达到 count 但 hasMore=true；分页契约矛盾，请检查 API"
                )
            if has_more is None:
                if declared_total is not None and len(seen) == declared_total:
                    return messages
                if not page:
                    if declared_total is not None:
                        raise WeflowApiError(
                            f"WeFlow API /messages 空页提前结束：唯一消息 {len(seen)} 条，count={declared_total}；"
                            "请重试或检查 API 分页"
                        )
                    return messages
            offset = _next_offset(offset, len(page), "/api/v1/messages")

    def fetch_contacts(self, *, limit: int = 5000) -> list[dict[str, Any]]:
        return self._fetch_offset_collection(
            "/api/v1/contacts",
            "contacts",
            limit=limit,
            identity_fields=("username",),
        )

    def fetch_group_members(self, chatroom_id: str) -> list[dict[str, Any]]:
        payload = self._request(
            "/api/v1/group-members",
            {"chatroomId": chatroom_id},
        )
        return self._list_field(payload, "members")

    def iter_timeline(self, username: str, *, limit: int = 100) -> Iterator[dict[str, Any]]:
        """Use required server filtering, then enforce the same filter locally."""

        offset = 0
        seen: set[str] = set()
        while True:
            payload = self._request(
                "/api/v1/sns/timeline",
                {"limit": limit, "offset": offset, "usernames": username},
            )
            page = self._list_field(payload, "timeline")
            for post in page:
                if str(post.get("username") or "") != username:
                    continue
                key = str(post.get("tid") or post.get("id") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                yield post
            if len(page) < limit:
                return
            offset += len(page)

    def export_moments(
        self,
        output_dir: str | Path,
        usernames: list[str],
        *,
        start: date | str,
        end: date | str,
    ) -> dict[str, Any]:
        """Ask WeFlow to export one exact moments range into a staging directory."""

        destination = Path(output_dir).resolve()
        targets = [str(value).strip() for value in usernames if str(value).strip()]
        if not targets:
            raise ValueError("朋友圈导出 usernames 不能为空，拒绝退化为全量导出")
        payload = self._request(
            "/api/v1/sns/export",
            method="POST",
            body={
                "outputDir": str(destination),
                "usernames": targets,
                "start": _api_date(start),
                "end": _api_date(end),
                # This exact key is required by WeFlow. Similar-looking names
                # silently return 200 while exporting no media.
                "exportMedia": True,
            },
        )
        if not payload.get("success"):
            raise WeflowApiError("WeFlow 朋友圈导出未返回 filePath")
        return payload

    def semantic_probe(self) -> tuple[str, int]:
        """Prove that a protected endpoint can read at least one message."""

        sessions = self.fetch_sessions(limit=2000)
        for session in sessions:
            talker = str(session.get("username") or "")
            if not talker:
                continue
            # The API requires dates; use the session's last local calendar day.
            timestamp = int(session.get("lastTimestamp") or 0)
            if timestamp <= 0:
                continue
            from datetime import datetime

            day = datetime.fromtimestamp(timestamp).date()
            payload = self.get_message_page(
                talker,
                start=day,
                end=day,
                limit=1,
                media=False,
            )
            messages = self._list_field(payload, "messages")
            if messages:
                return talker, len(messages)
        raise WeflowApiError(
            "API 探活成功，但未能从任何已知会话读到消息；请确认当前微信数据版本受 WeFlow API 支持。"
        )

    def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        headers = {"Accept": "application/json"}
        if authenticated and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=data, headers=headers, method=method)
        effective_timeout = self.timeout if timeout is None else timeout
        try:
            response = self._opener(request, timeout=effective_timeout)
            with response:
                raw = response.read()
        except HTTPError as exc:
            if exc.code == 401:
                raise WeflowApiError(
                    "WeFlow API Access Token 缺失或不匹配；固定 token 后请重启 API 服务。",
                    status=401,
                ) from exc
            message = f"WeFlow API HTTP {exc.code}: {path}"
            detail = _http_error_detail(exc, access_token=self.access_token)
            if detail:
                message += f" — {detail}"
            raise WeflowApiError(message, status=exc.code) from exc
        except (URLError, OSError, TimeoutError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else None
            if isinstance(exc, TimeoutError) or isinstance(reason, (socket.timeout, TimeoutError)):
                timeout_text = f"{effective_timeout:g}"
                if path == "/api/v1/messages":
                    raise WeflowApiError(
                        f"请求 WeFlow API 消息数据超时：本次请求超过 {timeout_text} 秒。"
                        "建议缩小日期范围，或调大 "
                        "[export_backend.weflow_api].message_request_timeout_sec"
                        f"（当前 {timeout_text} 秒）。"
                    ) from exc
                raise WeflowApiError(
                    f"请求 WeFlow API 控制面超时：本次请求超过 {timeout_text} 秒。"
                    "请检查 API 服务是否响应；如持续超时，请重启 WeFlow API 服务后重试。"
                    "也可检查 [export_backend.weflow_api].request_timeout_sec"
                    f"（当前 {timeout_text} 秒）。"
                ) from exc
            raise WeflowApiError(f"无法连接 WeFlow API {self.base_url}：{exc}") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WeflowApiError(f"WeFlow API 返回的不是有效 JSON：{path}") from exc
        if not isinstance(payload, dict):
            raise WeflowApiError(f"WeFlow API 响应形状错误（应为 object）：{path}")
        if authenticated and payload.get("success") is False:
            raise WeflowApiError(str(payload.get("error") or f"WeFlow API 请求失败：{path}"))
        return payload

    def _fetch_offset_collection(
        self,
        path: str,
        field: str,
        *,
        limit: int,
        identity_fields: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("分页 limit 必须大于 0")
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        offset = 0
        declared_total: int | None = None
        while True:
            payload = self._request(path, {"limit": limit, "offset": offset})
            page = self._list_field(payload, field)
            page_keys: set[tuple[str, ...]] = set()
            for item in page:
                key = tuple(str(item.get(name) or "") for name in identity_fields)
                if not any(key):
                    joined = "/".join(identity_fields)
                    raise WeflowApiError(
                        f"WeFlow API {path} 分页项缺少 {joined}；无法证明结果完整，请升级或检查 API"
                    )
                if key in page_keys or key in seen:
                    raise WeflowApiError(
                        f"WeFlow API {path} 返回重复 ID；已停止以避免死循环或重复导出，请检查 API 分页"
                    )
                page_keys.add(key)
            seen.update(page_keys)
            items.extend(page)

            page_total = _declared_total(payload, path)
            if page_total is not None:
                if declared_total is None:
                    declared_total = page_total
                elif declared_total != page_total:
                    raise WeflowApiError(
                        f"WeFlow API {path} 的 count 在分页间变化；无法证明结果完整，请重试或检查 API"
                    )
                if len(seen) > declared_total:
                    raise WeflowApiError(
                        f"WeFlow API {path} 唯一 ID 数超过 count；分页契约矛盾，请检查 API"
                    )
                if len(seen) == declared_total:
                    return items

            if not page:
                if declared_total is not None:
                    raise WeflowApiError(
                        f"WeFlow API {path} 空页提前结束：唯一 ID {len(seen)} 个，count={declared_total}；"
                        "请重试或检查 API 分页"
                    )
                return items
            offset = _next_offset(offset, len(page), path)

    @staticmethod
    def _list_field(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
        value = payload.get(key)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise WeflowApiError(f"WeFlow API 响应缺少 {key}[]")
        return value


def _api_date(value: date | str) -> str:
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = str(value).replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"日期必须为 YYYYMMDD 或 YYYY-MM-DD：{value!r}")
    return text


def _declared_total(payload: dict[str, Any], path: str) -> int | None:
    if "count" not in payload or payload.get("count") is None:
        return None
    raw = payload.get("count")
    if isinstance(raw, bool):
        raise WeflowApiError(f"WeFlow API {path} 的 count 不是非负整数；请检查 API 分页契约")
    text = str(raw).strip()
    if not text.isdigit():
        raise WeflowApiError(f"WeFlow API {path} 的 count 不是非负整数；请检查 API 分页契约")
    return int(text)


def _declared_has_more(payload: dict[str, Any], path: str) -> bool | None:
    if "hasMore" not in payload or payload.get("hasMore") is None:
        return None
    value = payload.get("hasMore")
    if not isinstance(value, bool):
        raise WeflowApiError(f"WeFlow API {path} 的 hasMore 不是 boolean；请检查 API 分页契约")
    return value


def _next_offset(offset: int, page_size: int, path: str) -> int:
    next_offset = offset + page_size
    if page_size <= 0 or next_offset <= offset:
        raise WeflowApiError(f"WeFlow API {path} 分页 offset 未前进；已停止以避免死循环")
    return next_offset


def _http_error_detail(exc: HTTPError, *, access_token: str) -> str:
    try:
        raw = exc.read(MAX_HTTP_ERROR_BODY_BYTES + 1)
    except Exception:
        return ""
    if len(raw) > MAX_HTTP_ERROR_BODY_BYTES:
        return ""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), str):
        return ""
    detail = " ".join(payload["error"].split())
    if access_token:
        detail = detail.replace(access_token, "[REDACTED]")
    if len(detail) > MAX_HTTP_ERROR_DETAIL_CHARS:
        detail = detail[: MAX_HTTP_ERROR_DETAIL_CHARS - 1].rstrip() + "…"
    return detail


__all__ = ["WeflowApiClient", "WeflowApiError"]
