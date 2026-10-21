"""One identity comparison shared by archive folding and overwrite guards."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from collections import Counter
from dataclasses import dataclass

from .media_store import MEDIA_SCHEME_KEY, MEDIA_SCHEME_VALUE


@dataclass(frozen=True)
class IdentityComparison:
    incoming_lineage: str | None
    archived_lineage: str | None
    same_lineage: bool
    basis: str
    incoming: Counter
    archived: Counter
    incoming_missing: int = 0
    archived_missing: int = 0
    same_session: bool = True

    @property
    def comparable(self) -> bool:
        return self.same_session and (self.same_lineage or (
            not self.incoming_missing and not self.archived_missing
            and bool(self.incoming & self.archived)))

    @property
    def covered(self) -> bool:
        return self.comparable and not (self.archived - self.incoming)

    def to_dict(self) -> dict:
        return {"comparable": self.comparable, "covered": self.covered,
                "basis": self.basis,
                "missing": sum((self.archived - self.incoming).values()),
                "incomingMissing": self.incoming_missing,
                "archivedMissing": self.archived_missing,
                "incomingLineage": self.incoming_lineage,
                "archivedLineage": self.archived_lineage}


def compare_chat_identities(incoming: dict, archived: dict) -> IdentityComparison:
    first, second = _chat_lineage(incoming), _chat_lineage(archived)
    same = first is not None and first == second
    missing_first = missing_second = 0
    if same:
        label = "(createTime, localId)"
        a = _local_identity_counter(incoming["messages"])
        b = _local_identity_counter(archived["messages"])
    else:
        label = "platformMessageId"
        a, missing_first = _platform_identity_counter(incoming["messages"])
        b, missing_second = _platform_identity_counter(archived["messages"])
    return IdentityComparison(first, second, same, label, a, b,
                              missing_first, missing_second,
                              str(incoming["session"].get("wxid") or "") ==
                              str(archived["session"].get("wxid") or ""))


def _chat_regression_reason(incoming: dict, archived: dict) -> tuple[str, bool] | None:
    incoming_session = incoming["session"]
    archived_session = archived["session"]
    incoming_wxid = str(incoming_session.get("wxid") or "")
    archived_wxid = str(archived_session.get("wxid") or "")
    if incoming_wxid != archived_wxid:
        return (
            f"会话身份不一致（incoming wxid={incoming_wxid!r}, archived wxid={archived_wxid!r}）",
            False,
        )

    incoming_messages = incoming["messages"]
    archived_messages = archived["messages"]
    comparison = compare_chat_identities(incoming, archived)
    incoming_lineage = comparison.incoming_lineage
    archived_lineage = comparison.archived_lineage
    same_lineage = comparison.same_lineage
    identity_label = comparison.basis
    incoming_identities = comparison.incoming
    archived_identities = comparison.archived
    if not same_lineage:
        incoming_missing = comparison.incoming_missing
        archived_missing = comparison.archived_missing
        if incoming_missing or archived_missing:
            return (
                "跨代消息身份不可比：platformMessageId 存在空值"
                f"（incoming={incoming_missing}, archived={archived_missing}）",
                True,
            )
        if not (incoming_identities & archived_identities):
            return (
                "跨代消息身份不可比：platformMessageId 零交集"
                f"（incoming lineage={incoming_lineage or 'unknown'}, "
                f"archived lineage={archived_lineage or 'unknown'}）",
                True,
            )

    incoming_max = _max_create_time(incoming_messages)
    archived_max = _max_create_time(archived_messages)
    if incoming_max < archived_max:
        return f"incoming 时间水位更旧（max createTime {incoming_max} < {archived_max}）", False

    missing_from_incoming = archived_identities - incoming_identities
    if missing_from_incoming:
        return (
            f"incoming 未覆盖 archived 的全部消息身份（按 {identity_label} 多重集合缺 "
            f"{sum(missing_from_incoming.values())} 条）",
            False,
        )

    shared_messages_changed = same_lineage and _shared_messages_changed(
        incoming_messages,
        archived_messages,
    )
    if same_lineage and incoming_lineage == "legacy_gui":
        incoming_exported_at = _legacy_exported_at(incoming)
        archived_exported_at = _legacy_exported_at(archived)
        if incoming_exported_at is not None and archived_exported_at is not None:
            if incoming_exported_at < archived_exported_at:
                return (
                    "incoming legacy 快照 exportedAt 更旧"
                    f"（{incoming_exported_at} < {archived_exported_at}）",
                    False,
                )
            if shared_messages_changed and incoming_exported_at > archived_exported_at:
                return None

    if shared_messages_changed:
        return "同代共同消息内容不同，缺少可信快照版本字段，无法证明 incoming 更新", True
    return None


def _chat_lineage(data: dict) -> str | None:
    metadata = data.get("weflow")
    if not isinstance(metadata, dict):
        return None
    source = str(metadata.get("source") or "").strip().casefold()
    if source == "http_api":
        media_scheme = str(metadata.get(MEDIA_SCHEME_KEY) or "").strip()
        if not media_scheme or media_scheme == MEDIA_SCHEME_VALUE:
            return "http_api"
        return None
    generator = str(metadata.get("generator") or "").strip().casefold()
    if generator == "weflow":
        return "legacy_gui"
    return None


def _legacy_exported_at(data: dict) -> int | None:
    metadata = data.get("weflow")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("exportedAt")
    if isinstance(value, bool):
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if timestamp > 0 else None


def _local_identity_counter(messages: list) -> Counter[tuple[int, str]]:
    return Counter(
        (_numeric_create_time(message), str(message.get("localId")))
        for message in messages
        if isinstance(message, dict)
    )


def _platform_identity_counter(messages: list) -> tuple[Counter[str], int]:
    identities: Counter[str] = Counter()
    missing = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        value = message.get("platformMessageId")
        if value is None or str(value) == "":
            missing += 1
        else:
            identities[str(value)] += 1
    return identities, missing


def _max_create_time(messages: list) -> int:
    return max((_numeric_create_time(message) for message in messages if isinstance(message, dict)), default=0)


def _numeric_create_time(message: dict) -> int:
    try:
        return int(message.get("createTime") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _shared_messages_changed(incoming_messages: list, archived_messages: list) -> bool:
    incoming_by_identity = _messages_by_local_identity(incoming_messages)
    archived_by_identity = _messages_by_local_identity(archived_messages)
    for identity in incoming_by_identity.keys() & archived_by_identity.keys():
        if incoming_by_identity[identity] != archived_by_identity[identity]:
            return True
    return False


def _messages_by_local_identity(messages: list) -> dict[tuple[int, str], list[dict]]:
    grouped: dict[tuple[int, str], list[dict]] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        identity = (_numeric_create_time(message), str(message.get("localId")))
        grouped.setdefault(identity, []).append(message)
    return grouped



def _files_equal(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_fh, right.open("rb") as right_fh:
        while True:
            left_chunk = left_fh.read(1024 * 1024)
            right_chunk = right_fh.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _collision_failure_reason(source: Path, destination: Path, *, range_media_root: Path | None = None,
                              range_media_objects: dict[Path, Path] | None = None) -> tuple[str, bool] | None:
    if source.suffix.lower() != ".json" or destination.suffix.lower() != ".json":
        return "同路径非 JSON 文件内容不同，无法从文件内容证明 incoming 更新", True
    try:
        incoming = _read_json(source)
        archived = _read_json(destination)
    except (OSError, json.JSONDecodeError) as exc:
        return f"同路径 JSON 无法比较（{type(exc).__name__}）", True
    both_chat_json = _is_chat_json(incoming) and _is_chat_json(archived)
    if not both_chat_json:
        # Range snapshots already have a collision rule: covered posts and
        # landed media permit replacement. Reuse it at final archive inspection.
        from .moments_ranges import range_digest, compare_coverage
        if range_digest(source) and range_digest(source) == range_digest(destination):
            coverage = compare_coverage(destination, source, new_media_root=range_media_root or source.parent,
                                        old_media_root=range_media_root, media_objects=range_media_objects)
            if coverage.covered:
                return None
            return '朋友圈区间未覆盖旧动态或媒体', False
        return "同路径非聊天 JSON 内容不同，没有可用的消息身份契约", True
    if incoming == archived:
        return None
    return _chat_regression_reason(incoming, archived)


def _read_json(path: Path) -> object:
    with path.open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def _is_chat_json(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("session"), dict)
        and isinstance(value.get("messages"), list)
    )



_SESSION_PREFIX_CHUNK_CHARS = 4096
_SESSION_PREFIX_MAX_CHARS = 65536
_NEED_MORE_PREFIX = object()
_USE_FULL_DOCUMENT = object()


def _read_stored_session(json_path: Path) -> dict[str, Any] | None:
    """Read the leading canonical session object without materializing messages.

    Canonical exports serialize ``session`` as the first top-level field. Any
    other ordering, oversized metadata, or ambiguous prefix falls back to the
    previous full-document parser so the optimization cannot hide a supported
    historical shape.
    """

    try:
        with json_path.open("r", encoding="utf-8-sig") as stream:
            prefix = ""
            while len(prefix) < _SESSION_PREFIX_MAX_CHARS:
                chunk = stream.read(
                    min(
                        _SESSION_PREFIX_CHUNK_CHARS,
                        _SESSION_PREFIX_MAX_CHARS - len(prefix),
                    )
                )
                prefix += chunk
                parsed = _parse_leading_session(prefix)
                if isinstance(parsed, dict):
                    return parsed
                if parsed is _USE_FULL_DOCUMENT or not chunk:
                    break
    except (OSError, UnicodeDecodeError):
        return None

    try:
        payload = json.loads(json_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("session"), dict):
        return None
    return payload["session"]


def _parse_leading_session(prefix: str) -> dict[str, Any] | object:
    """Return a complete first ``session`` value or a parser control sentinel."""

    decoder = json.JSONDecoder()
    index = 0
    while index < len(prefix) and prefix[index].isspace():
        index += 1
    if index >= len(prefix):
        return _NEED_MORE_PREFIX
    if prefix[index] != "{":
        return _USE_FULL_DOCUMENT
    index += 1
    while index < len(prefix) and prefix[index].isspace():
        index += 1
    try:
        key, index = decoder.raw_decode(prefix, index)
    except json.JSONDecodeError:
        return _NEED_MORE_PREFIX
    if key != "session":
        return _USE_FULL_DOCUMENT
    while index < len(prefix) and prefix[index].isspace():
        index += 1
    if index >= len(prefix):
        return _NEED_MORE_PREFIX
    if prefix[index] != ":":
        return _USE_FULL_DOCUMENT
    index += 1
    while index < len(prefix) and prefix[index].isspace():
        index += 1
    try:
        session, _ = decoder.raw_decode(prefix, index)
    except json.JSONDecodeError:
        return _NEED_MORE_PREFIX
    return session if isinstance(session, dict) else _USE_FULL_DOCUMENT


