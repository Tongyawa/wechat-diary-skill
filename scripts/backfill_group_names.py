"""Backfill unresolved group member names in canonical raw without calling WeFlow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


_DERIVED_MESSAGE_FIELDS = (
    "senderDisplayName",
    "senderNickname",
    "senderRemark",
    "quotedSenderDisplayName",
)


class BackfillError(RuntimeError):
    pass


@dataclass(frozen=True)
class NameCandidate:
    display_name: str
    nickname: str
    remark: str
    timestamp: int

    @property
    def rank(self) -> tuple[int, int, str, str, str]:
        richness = int(bool(self.nickname)) + int(bool(self.remark))
        return (self.timestamp, richness, self.display_name, self.nickname, self.remark)


@dataclass(frozen=True)
class DocumentRef:
    path: Path
    digest: str
    session_wxid: str


@dataclass(frozen=True)
class FilePatch:
    path: Path
    original: bytes
    updated: bytes
    repaired_messages: int
    sender_display_names: int
    sender_nicknames: int
    sender_remarks: int
    quoted_display_names: int


@dataclass(frozen=True)
class BackfillReport:
    scanned_json_files: int
    group_json_files: int
    roster_sessions: int
    roster_names: int
    affected_files: int
    repaired_messages: int
    sender_display_names: int
    sender_nicknames: int
    sender_remarks: int
    quoted_display_names: int
    unresolved_sender_names: int
    unresolved_quoted_names: int
    applied: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="离线回填 canonical raw 中仍显示为 wxid 的群成员名字",
    )
    parser.add_argument("--raw-root", required=True, help="canonical raw 根目录")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写入；省略时只预览受影响文件和字段数",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = backfill_group_names(Path(args.raw_root), apply=args.apply)
    except (BackfillError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"群成员名字回填失败：{exc}", file=sys.stderr)
        return 1

    mode = "已写入" if report.applied else "仅预览（传 --apply 才会写入）"
    print(f"群成员名字回填：{mode}")
    print(
        "扫描："
        f"JSON {report.scanned_json_files}；"
        f"群聊 canonical {report.group_json_files}；"
        f"同群名字 {report.roster_names}（{report.roster_sessions} 个群）"
    )
    print(
        "可修复："
        f"文件 {report.affected_files}；"
        f"消息 {report.repaired_messages}；"
        f"senderDisplayName {report.sender_display_names}；"
        f"senderNickname {report.sender_nicknames}；"
        f"senderRemark {report.sender_remarks}；"
        f"quotedSenderDisplayName {report.quoted_display_names}"
    )
    print(
        "仍无同群名字证据："
        f"sender {report.unresolved_sender_names}；"
        f"quoted sender {report.unresolved_quoted_names}"
    )
    return 0


def backfill_group_names(raw_root: Path, *, apply: bool = False) -> BackfillReport:
    root = Path(raw_root).expanduser().resolve()
    if not root.is_dir():
        raise BackfillError(f"raw 根不存在或不是目录：{root}")

    paths = sorted(path for path in root.rglob("*.json") if path.is_file())
    rosters: dict[str, dict[str, NameCandidate]] = {}
    documents: list[DocumentRef] = []
    for path in paths:
        original, payload = _read_json(path)
        session_wxid = _group_session_wxid(payload, path)
        if session_wxid is None:
            continue
        documents.append(
            DocumentRef(
                path=path,
                digest=hashlib.sha256(original).hexdigest(),
                session_wxid=session_wxid,
            )
        )
        roster = rosters.setdefault(session_wxid, {})
        for message in payload["messages"]:
            if not isinstance(message, Mapping):
                raise BackfillError(f"群聊 canonical 的 messages 含非对象元素：{path}")
            _collect_candidates(roster, message)

    patches: list[FilePatch] = []
    unresolved_sender_names = 0
    unresolved_quoted_names = 0
    for document in documents:
        original, payload = _read_json(document.path)
        if hashlib.sha256(original).hexdigest() != document.digest:
            raise BackfillError(f"扫描期间文件发生变化，拒绝继续：{document.path}")
        if _group_session_wxid(payload, document.path) != document.session_wxid:
            raise BackfillError(f"扫描期间 session.wxid 发生变化，拒绝继续：{document.path}")
        patch, unresolved_sender, unresolved_quoted = _plan_file_patch(
            document.path,
            original,
            payload,
            rosters.get(document.session_wxid, {}),
        )
        unresolved_sender_names += unresolved_sender
        unresolved_quoted_names += unresolved_quoted
        if patch is not None:
            patches.append(patch)

    if apply:
        for patch in patches:
            _atomic_replace_bytes(patch.path, patch.updated)

    return BackfillReport(
        scanned_json_files=len(paths),
        group_json_files=len(documents),
        roster_sessions=sum(1 for roster in rosters.values() if roster),
        roster_names=sum(len(roster) for roster in rosters.values()),
        affected_files=len(patches),
        repaired_messages=sum(patch.repaired_messages for patch in patches),
        sender_display_names=sum(patch.sender_display_names for patch in patches),
        sender_nicknames=sum(patch.sender_nicknames for patch in patches),
        sender_remarks=sum(patch.sender_remarks for patch in patches),
        quoted_display_names=sum(patch.quoted_display_names for patch in patches),
        unresolved_sender_names=unresolved_sender_names,
        unresolved_quoted_names=unresolved_quoted_names,
        applied=apply,
    )


def _read_json(path: Path) -> tuple[bytes, dict[str, Any]]:
    original = path.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BackfillError(f"JSON 不是 UTF-8：{path}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BackfillError(f"JSON 不可解析：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise BackfillError(f"JSON 顶层不是对象：{path}")
    return original, payload


def _group_session_wxid(payload: Mapping[str, Any], path: Path) -> str | None:
    session = payload.get("session")
    if not isinstance(session, Mapping):
        return None
    session_wxid = str(session.get("wxid") or "").strip()
    if not session_wxid.endswith("@chatroom"):
        return None
    if not isinstance(payload.get("messages"), list):
        raise BackfillError(f"群聊 canonical 缺少 messages 数组：{path}")
    return session_wxid


def _collect_candidates(
    roster: dict[str, NameCandidate],
    message: Mapping[str, Any],
) -> None:
    timestamp = _integer(message.get("createTime"))
    sender = str(message.get("senderUsername") or "").strip()
    display_name = str(message.get("senderDisplayName") or "").strip()
    if sender and display_name and display_name != sender:
        _offer_candidate(
            roster,
            sender,
            NameCandidate(
                display_name=display_name,
                nickname=_known_value(message.get("senderNickname"), sender),
                remark=_known_value(message.get("senderRemark"), sender),
                timestamp=timestamp,
            ),
        )

    quoted_sender = str(message.get("quotedSender") or "").strip()
    quoted_display = str(message.get("quotedSenderDisplayName") or "").strip()
    if quoted_sender and quoted_display and quoted_display != quoted_sender:
        _offer_candidate(
            roster,
            quoted_sender,
            NameCandidate(
                display_name=quoted_display,
                nickname="",
                remark="",
                timestamp=timestamp,
            ),
        )


def _offer_candidate(
    roster: dict[str, NameCandidate],
    sender: str,
    candidate: NameCandidate,
) -> None:
    previous = roster.get(sender)
    if previous is None or candidate.rank > previous.rank:
        roster[sender] = candidate


def _plan_file_patch(
    path: Path,
    original: bytes,
    payload: dict[str, Any],
    roster: Mapping[str, NameCandidate],
) -> tuple[FilePatch | None, int, int]:
    text = original.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    working = text
    original_payload = deepcopy(payload)
    repaired_messages = 0
    sender_display_names = 0
    sender_nicknames = 0
    sender_remarks = 0
    quoted_display_names = 0
    unresolved_sender_names = 0
    unresolved_quoted_names = 0

    messages = payload["messages"]
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise BackfillError(f"群聊 canonical 的 messages 含非对象元素：{path}")
        updated = dict(message)
        changed = False

        sender = str(message.get("senderUsername") or "").strip()
        sender_display = str(message.get("senderDisplayName") or "").strip()
        if sender and sender_display == sender:
            candidate = roster.get(sender)
            if candidate is None:
                unresolved_sender_names += 1
            else:
                updated["senderDisplayName"] = candidate.display_name
                sender_display_names += 1
                changed = True
                if (
                    "senderNickname" in updated
                    and _is_unresolved_value(updated.get("senderNickname"), sender)
                    and candidate.nickname
                ):
                    updated["senderNickname"] = candidate.nickname
                    sender_nicknames += 1
                if (
                    "senderRemark" in updated
                    and _is_unresolved_value(updated.get("senderRemark"), sender)
                    and candidate.remark
                ):
                    updated["senderRemark"] = candidate.remark
                    sender_remarks += 1

        quoted_sender = str(message.get("quotedSender") or "").strip()
        quoted_display = str(message.get("quotedSenderDisplayName") or "").strip()
        if quoted_sender and quoted_display == quoted_sender:
            candidate = roster.get(quoted_sender)
            if candidate is None:
                unresolved_quoted_names += 1
            else:
                updated["quotedSenderDisplayName"] = candidate.display_name
                quoted_display_names += 1
                changed = True

        if not changed:
            continue
        old_fragment = _canonical_message_fragment(message, newline=newline)
        if working.count(old_fragment) != 1:
            raise BackfillError(
                "无法唯一定位 canonical 消息片段，拒绝整文件重序列化："
                f"{path}（messages[{index}]）"
            )
        new_fragment = _canonical_message_fragment(updated, newline=newline)
        working = working.replace(old_fragment, new_fragment, 1)
        messages[index] = updated
        repaired_messages += 1

    _assert_only_derived_fields_changed(original_payload, payload, path)
    updated_bytes = working.encode("utf-8")
    if updated_bytes == original:
        return None, unresolved_sender_names, unresolved_quoted_names
    return (
        FilePatch(
            path=path,
            original=original,
            updated=updated_bytes,
            repaired_messages=repaired_messages,
            sender_display_names=sender_display_names,
            sender_nicknames=sender_nicknames,
            sender_remarks=sender_remarks,
            quoted_display_names=quoted_display_names,
        ),
        unresolved_sender_names,
        unresolved_quoted_names,
    )


def _canonical_message_fragment(message: Mapping[str, Any], *, newline: str) -> str:
    dumped = json.dumps(dict(message), ensure_ascii=False, indent=2).replace("\n", newline)
    return newline.join(f"    {line}" for line in dumped.split(newline))


def _assert_only_derived_fields_changed(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    path: Path,
) -> None:
    normalized_after = deepcopy(after)
    before_messages = before.get("messages")
    after_messages = normalized_after.get("messages")
    if not isinstance(before_messages, list) or not isinstance(after_messages, list):
        raise BackfillError(f"canonical messages 结构不可比：{path}")
    if len(before_messages) != len(after_messages):
        raise BackfillError(f"回填改变了消息条数：{path}")
    for old_message, new_message in zip(before_messages, after_messages):
        if not isinstance(old_message, Mapping) or not isinstance(new_message, dict):
            raise BackfillError(f"canonical message 结构不可比：{path}")
        for field_name in _DERIVED_MESSAGE_FIELDS:
            if field_name in old_message:
                new_message[field_name] = old_message[field_name]
            else:
                new_message.pop(field_name, None)
    if normalized_after != before:
        raise BackfillError(f"回填试图修改名字派生字段之外的内容：{path}")


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _known_value(value: Any, sender: str) -> str:
    text = str(value or "").strip()
    return "" if not text or text == sender else text


def _is_unresolved_value(value: Any, sender: str) -> bool:
    return str(value or "").strip() in {"", sender}


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
