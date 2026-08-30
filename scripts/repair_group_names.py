"""Snapshot, refetch, recompute, and rescan group names in staged canonical raw."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_diary_core.backends.weflow_api.client import (
    GROUP_ROSTER_MAX_ATTEMPTS,
    GROUP_ROSTER_RETRY_COOLDOWN_SECONDS,
    WeflowApiClient,
)
from wechat_diary_core.config import Config, load_config
from wechat_diary_core.name_patch import (
    assert_only_message_fields_changed,
    atomic_replace_bytes,
    canonical_message_fragment,
)
from wechat_diary_core.raw_schema import RawSchemaError, validate_session_json
from wechat_diary_core.workspace_discovery import (
    WorkspaceResolutionError,
    resolve_config_path,
)


SNAPSHOT_FILENAME = "degraded-name-snapshot.json"
ROSTER_INDEX_FILENAME = "group-roster-index.json"
RECOMPUTE_REPORT_FILENAME = "group-name-recompute-report.json"
RESIDUAL_REPORT_FILENAME = "group-name-residual-report.json"

_ALLOWED_NAME_FIELDS = (
    "senderDisplayName",
    "senderRemark",
    "senderNickname",
    "senderGroupNickname",
    "quotedSenderDisplayName",
)


class GroupNameRepairError(RuntimeError):
    pass


class _EmptyRosterError(RuntimeError):
    pass


@dataclass(frozen=True)
class GroupDocument:
    path: Path
    relative_path: str
    original: bytes
    payload: dict[str, Any]
    session_wxid: str
    digest: str


@dataclass(frozen=True)
class FilePatch:
    path: Path
    original: bytes
    updated: bytes
    changed_messages: int
    sender_display_names: int
    sender_remarks: int
    sender_nicknames: int
    sender_group_nicknames: int
    quoted_display_names: int


@dataclass(frozen=True)
class FilePlan:
    path: Path
    relative_path: str
    current_digest: str
    updated_digest: str
    changed_messages: int
    sender_display_names: int
    sender_remarks: int
    sender_nicknames: int
    sender_group_nicknames: int
    quoted_display_names: int

    @property
    def affected(self) -> bool:
        return self.current_digest != self.updated_digest


@dataclass(frozen=True)
class PrepareReport:
    applied: bool
    group_files: int
    groups: int
    group_messages: int
    degraded_messages: int
    roster_successes: int
    empty_rosters: int
    exhausted_failures: int


@dataclass(frozen=True)
class RecomputeReport:
    applied: bool
    affected_files: int
    changed_messages: int
    sender_display_names: int
    sender_remarks: int
    sender_nicknames: int
    sender_group_nicknames: int
    quoted_display_names: int


class InterCallPacer:
    """Guarantee a cooldown after one roster call completes and before the next starts."""

    def __init__(
        self,
        *,
        interval_seconds: float = GROUP_ROSTER_RETRY_COOLDOWN_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if interval_seconds < GROUP_ROSTER_RETRY_COOLDOWN_SECONDS:
            raise ValueError(
                f"群名册批处理调用间隔不得小于 {GROUP_ROSTER_RETRY_COOLDOWN_SECONDS:g} 秒"
            )
        self.interval_seconds = interval_seconds
        self.monotonic = monotonic
        self.sleep = sleep
        self._last_completed_at: float | None = None

    def call(self, client: Any, chatroom_id: str) -> list[dict[str, Any]]:
        if self._last_completed_at is not None:
            elapsed = self.monotonic() - self._last_completed_at
            remaining = self.interval_seconds - elapsed
            if remaining > 0:
                self.sleep(remaining)
        try:
            return client.fetch_group_members(chatroom_id)
        finally:
            self._last_completed_at = self.monotonic()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按固定顺序修复暂存态 canonical raw 的群成员名字",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        help="先快照降级清单，再限速重取全部群名册；不修改 raw",
    )
    _add_common_paths(prepare)
    prepare.add_argument("--config", default=None, help="工作区 config.toml 路径")
    prepare.add_argument(
        "--apply",
        action="store_true",
        help="写入外部证据目录并实际调用名册 API；省略时只预览扫描计数",
    )

    recompute = subparsers.add_parser(
        "recompute",
        help="读取 prepare 证据，离线覆盖名册命中者的名字事实与派生字段",
    )
    _add_common_paths(recompute)
    recompute.add_argument(
        "--apply",
        action="store_true",
        help="实际写入 raw；省略时只预览字段变化数",
    )

    rescan = subparsers.add_parser(
        "rescan",
        help="按 senderDisplayName == senderUsername 复扫每群残量",
    )
    _add_common_paths(rescan)
    rescan.add_argument(
        "--apply",
        action="store_true",
        help="把复扫报告写入外部证据目录；省略时只输出到控制台",
    )
    return parser


def _add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--raw-root", required=True, help="暂存态 canonical raw 根")
    parser.add_argument(
        "--evidence-dir",
        required=True,
        help="工作区侧证据目录；不得位于公开代码仓或 raw 根内",
    )


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            client = None
            if args.apply:
                cfg = load_config(resolve_config_path(args.config))
                client = _make_client(cfg)
            report = prepare_group_name_repair(
                Path(args.raw_root),
                Path(args.evidence_dir),
                apply=args.apply,
                client=client,
            )
            _print_prepare_report(report)
        elif args.command == "recompute":
            report = recompute_group_names(
                Path(args.raw_root),
                Path(args.evidence_dir),
                apply=args.apply,
            )
            _print_recompute_report(report)
        else:
            report = rescan_group_names(
                Path(args.raw_root),
                Path(args.evidence_dir),
                write_report=args.apply,
            )
            _print_residual_report(report, written=args.apply)
    except (
        GroupNameRepairError,
        WorkspaceResolutionError,
        RawSchemaError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        print(f"暂存态群名字修复失败：{exc}", file=sys.stderr)
        return 1
    return 0


def prepare_group_name_repair(
    raw_root: Path,
    evidence_dir: Path,
    *,
    apply: bool = False,
    client: Any | None = None,
    pacer: InterCallPacer | None = None,
) -> PrepareReport:
    root = _resolve_raw_root(raw_root)
    evidence = _resolve_evidence_dir(
        evidence_dir,
        root,
        for_write=apply,
        require_exists=False,
    )
    snapshot = _build_degraded_snapshot(root)
    groups = sorted({item["sessionWxid"] for item in snapshot["files"]})

    if not apply:
        return PrepareReport(
            applied=False,
            group_files=len(snapshot["files"]),
            groups=len(groups),
            group_messages=int(snapshot["groupMessages"]),
            degraded_messages=len(snapshot["degradedMessages"]),
            roster_successes=0,
            empty_rosters=0,
            exhausted_failures=0,
        )
    if client is None:
        raise GroupNameRepairError("prepare --apply 需要可用的 WeFlow API client")

    reserved = [
        evidence / SNAPSHOT_FILENAME,
        evidence / ROSTER_INDEX_FILENAME,
        evidence / RECOMPUTE_REPORT_FILENAME,
        evidence / RESIDUAL_REPORT_FILENAME,
    ]
    existing = [path.name for path in reserved if path.exists()]
    if existing:
        raise GroupNameRepairError(
            "证据目录已含本工具产物，拒绝覆盖原始证据；请换一个新目录："
            + ", ".join(existing)
        )

    evidence.mkdir(parents=True, exist_ok=True)
    snapshot_path = evidence / SNAPSHOT_FILENAME
    _write_json(snapshot_path, snapshot)
    if not snapshot_path.is_file():
        raise GroupNameRepairError("降级清单未落盘，拒绝开始名册重取")

    index = _fetch_all_group_rosters(
        groups,
        evidence,
        client=client,
        pacer=pacer or InterCallPacer(),
    )
    statuses = [str(item["status"]) for item in index["groups"]]
    return PrepareReport(
        applied=True,
        group_files=len(snapshot["files"]),
        groups=len(groups),
        group_messages=int(snapshot["groupMessages"]),
        degraded_messages=len(snapshot["degradedMessages"]),
        roster_successes=statuses.count("success"),
        empty_rosters=statuses.count("empty_roster"),
        exhausted_failures=statuses.count("exhausted_failure"),
    )


def recompute_group_names(
    raw_root: Path,
    evidence_dir: Path,
    *,
    apply: bool = False,
) -> RecomputeReport:
    root = _resolve_raw_root(raw_root)
    evidence = _resolve_evidence_dir(evidence_dir, root, for_write=apply)
    snapshot_path = evidence / SNAPSHOT_FILENAME
    roster_index_path = evidence / ROSTER_INDEX_FILENAME
    if not snapshot_path.is_file():
        raise GroupNameRepairError("降级清单快照不存在；步骤 ① 未完成，拒绝写 raw")
    if not roster_index_path.is_file():
        raise GroupNameRepairError("群名册索引不存在；步骤 ② 未完成，拒绝写 raw")

    snapshot_bytes, snapshot = _read_json_object(snapshot_path)
    roster_index_bytes, roster_index = _read_json_object(roster_index_path)
    _validate_snapshot(snapshot, root)
    if roster_index.get("complete") is not True:
        raise GroupNameRepairError("群名册批量重取未完整收口，拒绝离线重算")
    _validate_roster_index_coverage(snapshot, roster_index)
    rosters = _load_successful_rosters(evidence, roster_index)
    snapshot_digest = _sha256(snapshot_bytes)
    roster_index_digest = _sha256(roster_index_bytes)
    accepted_after = _load_prior_after_digests(
        evidence / RECOMPUTE_REPORT_FILENAME,
        snapshot_digest=snapshot_digest,
        roster_index_digest=roster_index_digest,
    )
    plans = _preflight_recompute(
        root,
        snapshot,
        rosters,
        accepted_after=accepted_after,
    )
    report = _summarize_recompute(plans, applied=apply)

    if not apply or report.affected_files == 0:
        return report

    after_files: list[dict[str, str]] = []
    for plan in plans:
        if not plan.affected:
            after_files.append(
                {"path": plan.relative_path, "sha256": plan.current_digest}
            )
            continue
        original, payload = _read_json_object(plan.path)
        if _sha256(original) != plan.current_digest:
            raise GroupNameRepairError(f"预检后文件发生变化，拒绝继续：{plan.path}")
        session_wxid = _group_session_wxid(payload, plan.path)
        patch = _plan_file_patch(
            plan.path,
            original,
            payload,
            rosters.get(session_wxid or "", {}),
        )
        if patch is None or _sha256(patch.updated) != plan.updated_digest:
            raise GroupNameRepairError(f"预检与写入计划不一致，拒绝继续：{plan.path}")
        atomic_replace_bytes(plan.path, patch.updated)
        after_files.append({"path": plan.relative_path, "sha256": plan.updated_digest})

    payload = {
        "version": 1,
        "complete": True,
        "createdAt": _utc_now(),
        "snapshotSha256": snapshot_digest,
        "rosterIndexSha256": roster_index_digest,
        "afterFiles": after_files,
        "summary": _recompute_report_dict(report),
    }
    _write_json(evidence / RECOMPUTE_REPORT_FILENAME, payload)
    return report


def rescan_group_names(
    raw_root: Path,
    evidence_dir: Path,
    *,
    write_report: bool = False,
) -> dict[str, Any]:
    root = _resolve_raw_root(raw_root)
    evidence = _resolve_evidence_dir(
        evidence_dir,
        root,
        for_write=write_report,
        require_exists=False,
    )
    groups: dict[str, dict[str, Any]] = {}
    total_messages = 0
    total_unresolved = 0
    total_quoted = 0
    for document in _iter_group_documents(root):
        group = groups.setdefault(
            document.session_wxid,
            {
                "sessionWxid": document.session_wxid,
                "files": 0,
                "messages": 0,
                "unresolvedSenderMessages": 0,
                "unresolvedQuotedNames": 0,
            },
        )
        group["files"] += 1
        for message in document.payload["messages"]:
            if not isinstance(message, Mapping):
                raise GroupNameRepairError(
                    f"群聊 canonical 的 messages 含非对象元素：{document.path}"
                )
            group["messages"] += 1
            total_messages += 1
            if _is_degraded_sender(message):
                group["unresolvedSenderMessages"] += 1
                total_unresolved += 1
            if _is_degraded_quote(message):
                group["unresolvedQuotedNames"] += 1
                total_quoted += 1
    ordered_groups = []
    for group in sorted(groups.values(), key=lambda item: str(item["sessionWxid"])):
        group["residualRate"] = _rate(
            int(group["unresolvedSenderMessages"]), int(group["messages"])
        )
        ordered_groups.append(group)
    report = {
        "version": 1,
        "createdAt": _utc_now(),
        "rawRoot": str(root),
        "groups": ordered_groups,
        "totals": {
            "groups": len(ordered_groups),
            "messages": total_messages,
            "unresolvedSenderMessages": total_unresolved,
            "unresolvedQuotedNames": total_quoted,
            "residualRate": _rate(total_unresolved, total_messages),
        },
    }
    if write_report:
        evidence.mkdir(parents=True, exist_ok=True)
        _write_json(evidence / RESIDUAL_REPORT_FILENAME, report)
    return report


def _build_degraded_snapshot(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []
    group_messages = 0
    for document in _iter_group_documents(root):
        files.append(
            {
                "path": document.relative_path,
                "sha256": document.digest,
                "sessionWxid": document.session_wxid,
            }
        )
        for message in document.payload["messages"]:
            if not isinstance(message, Mapping):
                raise GroupNameRepairError(
                    f"群聊 canonical 的 messages 含非对象元素：{document.path}"
                )
            group_messages += 1
            if not _is_degraded_sender(message):
                continue
            degraded.append(
                {
                    "file": document.relative_path,
                    "localId": message.get("localId"),
                    "platformMessageId": message.get("platformMessageId"),
                    "sender": message.get("senderUsername"),
                    "sessionWxid": document.session_wxid,
                }
            )
    return {
        "version": 1,
        "complete": True,
        "createdAt": _utc_now(),
        "rawRoot": str(root),
        "groupMessages": group_messages,
        "files": files,
        "degradedMessages": degraded,
    }


def _fetch_all_group_rosters(
    groups: Sequence[str],
    evidence: Path,
    *,
    client: Any,
    pacer: InterCallPacer,
) -> dict[str, Any]:
    index_path = evidence / ROSTER_INDEX_FILENAME
    payload: dict[str, Any] = {
        "version": 1,
        "complete": False,
        "createdAt": _utc_now(),
        "groups": [],
    }
    _write_json(index_path, payload)
    for session_wxid in groups:
        status, members, error, attempts = _fetch_one_group_roster(
            client,
            session_wxid,
            pacer=pacer,
        )
        entry: dict[str, Any] = {
            "sessionWxid": session_wxid,
            "status": status,
            "attempts": attempts,
        }
        if status == "success":
            snapshot_name = _roster_snapshot_name(session_wxid)
            snapshot_bytes = json.dumps(members, ensure_ascii=False, indent=2).encode(
                "utf-8"
            )
            atomic_replace_bytes(evidence / snapshot_name, snapshot_bytes)
            entry.update(
                {
                    "snapshotFile": snapshot_name,
                    "snapshotSha256": _sha256(snapshot_bytes),
                    "members": len(members),
                }
            )
        else:
            entry["error"] = error
        payload["groups"].append(entry)
        _write_json(index_path, payload)
    payload["complete"] = True
    payload["completedAt"] = _utc_now()
    _write_json(index_path, payload)
    return payload


def _fetch_one_group_roster(
    client: Any,
    session_wxid: str,
    *,
    pacer: InterCallPacer,
) -> tuple[str, list[dict[str, Any]], str, int]:
    last_error: Exception | None = None
    for attempt in range(1, GROUP_ROSTER_MAX_ATTEMPTS + 1):
        try:
            members = pacer.call(client, session_wxid)
            if not members:
                raise _EmptyRosterError("群成员接口返回空名册")
            _validate_roster_members(members, session_wxid)
            return "success", members, "", attempt
        except Exception as exc:
            last_error = exc
    assert last_error is not None
    status = "empty_roster" if isinstance(last_error, _EmptyRosterError) else "exhausted_failure"
    return status, [], f"{type(last_error).__name__}: {last_error}", GROUP_ROSTER_MAX_ATTEMPTS


def _validate_roster_members(members: Any, session_wxid: str) -> None:
    if not isinstance(members, list):
        raise GroupNameRepairError(f"群名册响应不是数组：{session_wxid}")
    seen: set[str] = set()
    for index, member in enumerate(members):
        if not isinstance(member, Mapping):
            raise GroupNameRepairError(
                f"群名册成员不是对象：{session_wxid} members[{index}]"
            )
        wxid = str(member.get("wxid") or "").strip()
        if not wxid:
            raise GroupNameRepairError(
                f"群名册成员缺少 wxid：{session_wxid} members[{index}]"
            )
        if wxid in seen:
            raise GroupNameRepairError(f"群名册含重复 wxid：{session_wxid}")
        seen.add(wxid)


def _load_successful_rosters(
    evidence: Path,
    roster_index: Mapping[str, Any],
) -> dict[str, dict[str, Mapping[str, Any]]]:
    groups = roster_index.get("groups")
    if not isinstance(groups, list):
        raise GroupNameRepairError("群名册索引缺少 groups 数组")
    rosters: dict[str, dict[str, Mapping[str, Any]]] = {}
    seen_groups: set[str] = set()
    for item in groups:
        if not isinstance(item, Mapping):
            raise GroupNameRepairError("群名册索引 groups 含非对象元素")
        session_wxid = str(item.get("sessionWxid") or "").strip()
        if not session_wxid or session_wxid in seen_groups:
            raise GroupNameRepairError("群名册索引含空或重复 sessionWxid")
        seen_groups.add(session_wxid)
        if item.get("status") != "success":
            continue
        relative = str(item.get("snapshotFile") or "")
        snapshot_path = _safe_evidence_child(evidence, relative)
        content = snapshot_path.read_bytes()
        if _sha256(content) != str(item.get("snapshotSha256") or ""):
            raise GroupNameRepairError(f"群名册快照摘要不匹配：{snapshot_path}")
        members = json.loads(content.decode("utf-8"))
        _validate_roster_members(members, session_wxid)
        rosters[session_wxid] = {
            str(member["wxid"]).strip(): member for member in members
        }
    return rosters


def _preflight_recompute(
    root: Path,
    snapshot: Mapping[str, Any],
    rosters: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    accepted_after: Mapping[str, str],
) -> list[FilePlan]:
    manifest_files = snapshot.get("files")
    if not isinstance(manifest_files, list):
        raise GroupNameRepairError("降级清单缺少 files 数组")
    expected: dict[str, Mapping[str, Any]] = {}
    for item in manifest_files:
        if not isinstance(item, Mapping):
            raise GroupNameRepairError("降级清单 files 含非对象元素")
        relative = str(item.get("path") or "")
        if not relative or relative in expected:
            raise GroupNameRepairError("降级清单含空或重复文件路径")
        expected[relative] = item

    plans: list[FilePlan] = []
    seen: set[str] = set()
    for document in _iter_group_documents(root):
        item = expected.get(document.relative_path)
        if item is None:
            raise GroupNameRepairError(
                f"快照后新增群聊 canonical，拒绝重算：{document.relative_path}"
            )
        seen.add(document.relative_path)
        if document.session_wxid != str(item.get("sessionWxid") or ""):
            raise GroupNameRepairError(
                f"快照后 session.wxid 发生变化：{document.relative_path}"
            )
        original_digest = str(item.get("sha256") or "")
        allowed_digests = {original_digest}
        prior_after = accepted_after.get(document.relative_path)
        if prior_after:
            allowed_digests.add(prior_after)
        if document.digest not in allowed_digests:
            raise GroupNameRepairError(
                f"快照后文件发生非本工具改动，拒绝重算：{document.relative_path}"
            )
        patch = _plan_file_patch(
            document.path,
            document.original,
            document.payload,
            rosters.get(document.session_wxid, {}),
        )
        updated_digest = document.digest if patch is None else _sha256(patch.updated)
        plans.append(
            FilePlan(
                path=document.path,
                relative_path=document.relative_path,
                current_digest=document.digest,
                updated_digest=updated_digest,
                changed_messages=patch.changed_messages if patch else 0,
                sender_display_names=patch.sender_display_names if patch else 0,
                sender_remarks=patch.sender_remarks if patch else 0,
                sender_nicknames=patch.sender_nicknames if patch else 0,
                sender_group_nicknames=patch.sender_group_nicknames if patch else 0,
                quoted_display_names=patch.quoted_display_names if patch else 0,
            )
        )
    missing = sorted(set(expected) - seen)
    if missing:
        raise GroupNameRepairError(f"快照后群聊 canonical 缺失：{missing[0]}")
    return plans


def _plan_file_patch(
    path: Path,
    original: bytes,
    payload: dict[str, Any],
    roster: Mapping[str, Mapping[str, Any]],
) -> FilePatch | None:
    text = original.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    working = text
    original_payload = deepcopy(payload)
    changed_messages = 0
    counters = {field: 0 for field in _ALLOWED_NAME_FIELDS}

    for index, message in enumerate(payload["messages"]):
        if not isinstance(message, dict):
            raise GroupNameRepairError(f"群聊 canonical 的 messages 含非对象元素：{path}")
        updated = dict(message)
        sender = str(message.get("senderUsername") or "").strip()
        member = roster.get(sender)
        if sender and member is not None:
            fact_values = {
                "senderRemark": _raw_name(member.get("remark")),
                "senderNickname": _raw_name(member.get("nickname")),
                "senderGroupNickname": _raw_name(member.get("groupNickname")),
            }
            for field_name, value in fact_values.items():
                if field_name not in updated or updated.get(field_name) != value:
                    updated[field_name] = value
                    counters[field_name] += 1
            display_name = _recomputed_display_name(
                member,
                fallback=str(message.get("senderDisplayName") or ""),
                wxid=sender,
            )
            if updated.get("senderDisplayName") != display_name:
                updated["senderDisplayName"] = display_name
                counters["senderDisplayName"] += 1

        quoted_sender = str(message.get("quotedSender") or "").strip()
        quoted_member = roster.get(quoted_sender)
        if quoted_member is not None and _is_degraded_quote(message):
            quoted_display = _recomputed_display_name(
                quoted_member,
                fallback=quoted_sender,
                wxid=quoted_sender,
            )
            if updated.get("quotedSenderDisplayName") != quoted_display:
                updated["quotedSenderDisplayName"] = quoted_display
                counters["quotedSenderDisplayName"] += 1

        if updated == message:
            continue
        old_fragment = canonical_message_fragment(message, newline=newline)
        if working.count(old_fragment) != 1:
            raise GroupNameRepairError(
                "无法唯一定位 canonical 消息片段，拒绝整文件重序列化："
                f"{path}（messages[{index}]）"
            )
        new_fragment = canonical_message_fragment(updated, newline=newline)
        working = working.replace(old_fragment, new_fragment, 1)
        payload["messages"][index] = updated
        changed_messages += 1

    _assert_only_name_fields_changed(original_payload, payload, path)
    validate_session_json(payload)
    updated_bytes = working.encode("utf-8")
    if updated_bytes == original:
        return None
    return FilePatch(
        path=path,
        original=original,
        updated=updated_bytes,
        changed_messages=changed_messages,
        sender_display_names=counters["senderDisplayName"],
        sender_remarks=counters["senderRemark"],
        sender_nicknames=counters["senderNickname"],
        sender_group_nicknames=counters["senderGroupNickname"],
        quoted_display_names=counters["quotedSenderDisplayName"],
    )


def _assert_only_name_fields_changed(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    path: Path,
) -> None:
    assert_only_message_fields_changed(
        before,
        after,
        path,
        allowed_fields=_ALLOWED_NAME_FIELDS,
        error_type=GroupNameRepairError,
    )


def _summarize_recompute(
    plans: Iterable[FilePlan],
    *,
    applied: bool,
) -> RecomputeReport:
    affected = [plan for plan in plans if plan.affected]
    return RecomputeReport(
        applied=applied,
        affected_files=len(affected),
        changed_messages=sum(plan.changed_messages for plan in affected),
        sender_display_names=sum(plan.sender_display_names for plan in affected),
        sender_remarks=sum(plan.sender_remarks for plan in affected),
        sender_nicknames=sum(plan.sender_nicknames for plan in affected),
        sender_group_nicknames=sum(
            plan.sender_group_nicknames for plan in affected
        ),
        quoted_display_names=sum(plan.quoted_display_names for plan in affected),
    )


def _recompute_report_dict(report: RecomputeReport) -> dict[str, Any]:
    return {
        "applied": report.applied,
        "affectedFiles": report.affected_files,
        "changedMessages": report.changed_messages,
        "senderDisplayNames": report.sender_display_names,
        "senderRemarks": report.sender_remarks,
        "senderNicknames": report.sender_nicknames,
        "senderGroupNicknames": report.sender_group_nicknames,
        "quotedDisplayNames": report.quoted_display_names,
    }


def _load_prior_after_digests(
    path: Path,
    *,
    snapshot_digest: str,
    roster_index_digest: str,
) -> dict[str, str]:
    if not path.is_file():
        return {}
    _, payload = _read_json_object(path)
    if (
        payload.get("complete") is not True
        or payload.get("snapshotSha256") != snapshot_digest
        or payload.get("rosterIndexSha256") != roster_index_digest
    ):
        return {}
    entries = payload.get("afterFiles")
    if not isinstance(entries, list):
        return {}
    result: dict[str, str] = {}
    for item in entries:
        if not isinstance(item, Mapping):
            return {}
        relative = str(item.get("path") or "")
        digest = str(item.get("sha256") or "")
        if not relative or not digest or relative in result:
            return {}
        result[relative] = digest
    return result


def _validate_snapshot(snapshot: Mapping[str, Any], root: Path) -> None:
    if snapshot.get("complete") is not True:
        raise GroupNameRepairError("降级清单快照未完整落盘")
    raw_root = Path(str(snapshot.get("rawRoot") or "")).expanduser().resolve()
    if raw_root != root:
        raise GroupNameRepairError(
            f"降级清单属于另一 raw 根：清单={raw_root}，本次={root}"
        )


def _validate_roster_index_coverage(
    snapshot: Mapping[str, Any],
    roster_index: Mapping[str, Any],
) -> None:
    files = snapshot.get("files")
    groups = roster_index.get("groups")
    if not isinstance(files, list) or not isinstance(groups, list):
        raise GroupNameRepairError("降级清单或群名册索引缺少群集合")
    expected = {
        str(item.get("sessionWxid") or "").strip()
        for item in files
        if isinstance(item, Mapping)
    }
    actual = {
        str(item.get("sessionWxid") or "").strip()
        for item in groups
        if isinstance(item, Mapping)
    }
    if "" in expected or "" in actual or expected != actual:
        raise GroupNameRepairError("群名册索引没有逐群覆盖降级清单，拒绝离线重算")


def _iter_group_documents(root: Path) -> Iterable[GroupDocument]:
    for path in sorted(candidate for candidate in root.rglob("*.json") if candidate.is_file()):
        original = path.read_bytes()
        try:
            payload = json.loads(original.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise GroupNameRepairError(f"JSON 不是 UTF-8：{path}") from exc
        except json.JSONDecodeError as exc:
            raise GroupNameRepairError(f"JSON 不可解析：{path}（{exc}）") from exc
        if not isinstance(payload, dict):
            continue
        session_wxid = _group_session_wxid(payload, path)
        if session_wxid is None:
            continue
        validate_session_json(payload)
        yield GroupDocument(
            path=path,
            relative_path=path.relative_to(root).as_posix(),
            original=original,
            payload=payload,
            session_wxid=session_wxid,
            digest=_sha256(original),
        )


def _group_session_wxid(payload: Mapping[str, Any], path: Path) -> str | None:
    session = payload.get("session")
    if not isinstance(session, Mapping):
        return None
    session_wxid = str(session.get("wxid") or "").strip()
    if not session_wxid.endswith("@chatroom"):
        return None
    if not isinstance(payload.get("messages"), list):
        raise GroupNameRepairError(f"群聊 canonical 缺少 messages 数组：{path}")
    return session_wxid


def _is_degraded_sender(message: Mapping[str, Any]) -> bool:
    sender = str(message.get("senderUsername") or "").strip()
    display = str(message.get("senderDisplayName") or "").strip()
    return bool(sender and display == sender)


def _is_degraded_quote(message: Mapping[str, Any]) -> bool:
    sender = str(message.get("quotedSender") or "").strip()
    display = str(message.get("quotedSenderDisplayName") or "").strip()
    return bool(sender and display == sender)


def _recomputed_display_name(
    member: Mapping[str, Any],
    *,
    fallback: str,
    wxid: str,
) -> str:
    return str(
        member.get("remark")
        or member.get("groupNickname")
        or member.get("nickname")
        or fallback
        or wxid
    )


def _raw_name(value: Any) -> str:
    return "" if value is None else str(value)


def _resolve_raw_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise GroupNameRepairError(f"raw 根不存在或不是目录：{resolved}")
    return resolved


def _resolve_evidence_dir(
    path: Path,
    raw_root: Path,
    *,
    for_write: bool,
    require_exists: bool = True,
) -> Path:
    evidence = path.expanduser().resolve()
    repository = ROOT.resolve()
    if _is_within(evidence, repository):
        raise GroupNameRepairError(f"证据目录不得位于公开代码仓内：{evidence}")
    if _is_within(evidence, raw_root) or _is_within(raw_root, evidence):
        raise GroupNameRepairError(f"证据目录必须与 raw 根分离：{evidence}")
    if require_exists and not for_write and not evidence.is_dir():
        raise GroupNameRepairError(f"证据目录不存在：{evidence}")
    return evidence


def _safe_evidence_child(evidence: Path, relative: str) -> Path:
    if not relative:
        raise GroupNameRepairError("群名册索引缺少 snapshotFile")
    path = (evidence / relative).resolve()
    if not _is_within(path, evidence) or path == evidence:
        raise GroupNameRepairError(f"群名册快照路径越界：{relative}")
    if not path.is_file():
        raise GroupNameRepairError(f"群名册快照不存在：{path}")
    return path


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _read_json_object(path: Path) -> tuple[bytes, dict[str, Any]]:
    content = path.read_bytes()
    payload = json.loads(content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise GroupNameRepairError(f"JSON 顶层不是对象：{path}")
    return content, payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_replace_bytes(path, content)


def _roster_snapshot_name(session_wxid: str) -> str:
    token = hashlib.sha256(session_wxid.encode("utf-8")).hexdigest()[:24]
    return f"group-members-{token}.json"


def _make_client(cfg: Config) -> WeflowApiClient:
    api = cfg.export_backend.weflow_api
    return WeflowApiClient(
        api.base_url,
        api.access_token,
        timeout=api.request_timeout_sec,
        message_timeout=api.message_request_timeout_sec,
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _print_prepare_report(report: PrepareReport) -> None:
    mode = "已落清单并重取名册" if report.applied else "仅预览（传 --apply 才落证据并调 API）"
    print(f"暂存态群名字 prepare：{mode}")
    print(
        f"群 {report.groups}；群聊 JSON {report.group_files}；消息 {report.group_messages}；"
        f"降级消息 {report.degraded_messages}"
    )
    if report.applied:
        print(
            f"名册：成功 {report.roster_successes}；空名册 {report.empty_rosters}；"
            f"重试耗尽 {report.exhausted_failures}"
        )


def _print_recompute_report(report: RecomputeReport) -> None:
    mode = "已写入" if report.applied else "仅预览（传 --apply 才写 raw）"
    print(f"暂存态群名字 recompute：{mode}")
    print(
        f"文件 {report.affected_files}；消息 {report.changed_messages}；"
        f"senderDisplayName {report.sender_display_names}；"
        f"senderRemark {report.sender_remarks}；senderNickname {report.sender_nicknames}；"
        f"senderGroupNickname {report.sender_group_nicknames}；"
        f"quotedSenderDisplayName {report.quoted_display_names}"
    )


def _print_residual_report(report: Mapping[str, Any], *, written: bool) -> None:
    totals = report["totals"]
    mode = "已写报告" if written else "仅输出（传 --apply 才写报告）"
    print(f"暂存态群名字 rescan：{mode}")
    for group in report["groups"]:
        print(
            f"- {group['sessionWxid']}：{group['unresolvedSenderMessages']}/"
            f"{group['messages']}（{group['residualRate']:.2%}）"
        )
    print(
        f"总计：{totals['unresolvedSenderMessages']}/{totals['messages']}"
        f"（{totals['residualRate']:.2%}）；未解析引用 {totals['unresolvedQuotedNames']}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
