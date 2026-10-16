"""Refetch bare appmsg/location content without replacing canonical exports."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime
import json
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_diary_core.backends.weflow_api.client import WeflowApiClient
from wechat_diary_core.backends.weflow_api.mapper import _message_content
from wechat_diary_core.backends.weflow_api.type_map import resolve_message_type, unpack_local_type
from wechat_diary_core.config import load_config
from wechat_diary_core.name_patch import atomic_replace_bytes
from wechat_diary_core.workspace_discovery import resolve_config_path


# Only placeholders whose current mapper can recover metadata. Unknown appmsg
# subtypes share [其他消息]; API localType disambiguates those after refetch.
BARE_LABELS = frozenset({
    "[链接]", "[音乐]", "[视频分享]", "[文件]", "[合并转发]", "[收藏]",
    "[小程序]", "[分享]", "[视频号]", "[动态]", "[接龙]", "[转账]", "[红包]",
    "[其他消息]", "[位置]",
})
STATUSES = ("candidate", "refillable", "refilled", "missing", "still_bare", "rejected", "api_failed")


class RefillError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RefillError("duplicate_json_key")
        result[key] = value
    return result


def _decode(raw: bytes) -> tuple[str, Any]:
    text = raw.decode("utf-8-sig")
    return text, json.loads(text, object_pairs_hook=_unique_object)


def _content_spans(text: str) -> dict[int, tuple[int, int]]:
    """Locate JSON string tokens by structural path, never by matching text."""
    decoder = json.JSONDecoder()
    spans: dict[int, tuple[int, int]] = {}

    def ws(pos: int) -> int:
        while pos < len(text) and text[pos] in " \r\n\t":
            pos += 1
        return pos

    def value(pos: int, path: tuple[Any, ...]) -> int:
        pos = ws(pos)
        start = pos
        if text[pos] == "{":
            pos = ws(pos + 1)
            while text[pos] != "}":
                key, pos = decoder.raw_decode(text, pos)
                pos = ws(pos)
                if text[pos] != ":":
                    raise RefillError("invalid_json_colon")
                pos = ws(value(pos + 1, (*path, key)))
                if text[pos] == "}":
                    break
                if text[pos] != ",":
                    raise RefillError("invalid_json_comma")
                pos = ws(pos + 1)
            return pos + 1
        if text[pos] == "[":
            pos = ws(pos + 1)
            index = 0
            while text[pos] != "]":
                pos = ws(value(pos, (*path, index)))
                index += 1
                if text[pos] == "]":
                    break
                if text[pos] != ",":
                    raise RefillError("invalid_json_comma")
                pos = ws(pos + 1)
            return pos + 1
        decoded, end = decoder.raw_decode(text, pos)
        if len(path) == 3 and path[0] == "messages" and path[2] == "content":
            if isinstance(decoded, str):
                spans[path[1]] = (start, end)
        return end

    value(0, ())
    return spans


def is_candidate(message: Any) -> bool:
    return (isinstance(message, dict) and message.get("type") == "其他消息"
            and message.get("content") in BARE_LABELS)


def _mask(text: str, spans: dict[int, tuple[int, int]], indexes: set[int]) -> str:
    for start, end in sorted((spans[i] for i in indexes), reverse=True):
        text = text[:start] + '"<content>"' + text[end:]
    return text


def verify_patch(before: bytes, after: bytes, replacements: dict[int, str]) -> None:
    """Independent write gate: exact semantics AND bytes outside chosen tokens."""
    old_text, old = _decode(before)
    new_text, new = _decode(after)
    expected = deepcopy(old)
    for index, content in replacements.items():
        message = old["messages"][index]
        if not is_candidate(message) or not content or content in BARE_LABELS:
            raise RefillError("not_a_content_repair")
        expected["messages"][index]["content"] = content
    if new != expected or before.startswith(b"\xef\xbb\xbf") != after.startswith(b"\xef\xbb\xbf"):
        raise RefillError("non_content_semantic_change")
    indexes = set(replacements)
    if _mask(old_text, _content_spans(old_text), indexes) != _mask(new_text, _content_spans(new_text), indexes):
        raise RefillError("non_content_byte_change")


def patch_content(original: bytes, replacements: dict[int, str]) -> bytes:
    text, _ = _decode(original)
    spans = _content_spans(text)
    for index in sorted(replacements, key=lambda i: spans[i][0], reverse=True):
        start, end = spans[index]
        text = text[:start] + json.dumps(replacements[index], ensure_ascii=False) + text[end:]
    updated = (b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")
    verify_patch(original, updated, replacements)
    return updated


def _key(message: dict[str, Any]) -> tuple[str, int]:
    local = message.get("localId")
    timestamp = message.get("createTime")
    if isinstance(local, bool) or local in (None, "") or isinstance(timestamp, bool):
        raise RefillError("missing_identity")
    # Reject floats rather than silently truncating an identity field.
    if not str(local).isdigit() or not str(timestamp).isdigit() or int(timestamp) <= 0:
        raise RefillError("invalid_identity")
    return str(int(local)), int(timestamp)


def _same_identity(old: dict[str, Any], fetched: dict[str, Any], talker: str) -> bool:
    """Verify message identity independently of whether its base is repairable."""
    platform_id = old.get("platformMessageId")
    if platform_id not in (None, "") and str(platform_id) != str(fetched.get("serverId", "")):
        return False
    if fetched.get("talker") not in (None, "", talker):
        return False
    is_send = fetched.get("isSend")
    if is_send not in (0, 1, False, True) or old.get("isSend") not in (0, 1, False, True):
        return False
    sender = str(fetched.get("senderUsername") or "")
    if not sender and (talker.endswith("@chatroom") or not is_send):
        sender = talker
    if not sender or sender != old.get("senderUsername") or int(is_send) != int(old["isSend"]):
        return False
    resolved = resolve_message_type(int(fetched.get("localType") or 0))
    return resolved.canonical == old["type"] and resolved.placeholder == old["content"]


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _check_backup(root: Path, backup: Path | None, apply: bool) -> Path | None:
    if apply and backup is None:
        raise RefillError("apply_requires_backup_dir")
    if backup is None:
        return None
    backup = backup.expanduser().resolve()
    if _inside(backup, root) or _inside(root, backup) or _inside(backup, ROOT):
        raise RefillError("backup_must_be_separate_from_raw_and_code")
    return backup


def refill(raw_root: Path, *, client: Any = None, apply: bool = False,
           backup_dir: Path | None = None, scan_only: bool = False,
           appmsg_text_max_chars: int = 300, progress: Any = None) -> dict[str, Any]:
    root = raw_root.expanduser().resolve()
    if not root.is_dir():
        raise RefillError("raw_root_not_directory")
    if scan_only and apply:
        raise RefillError("scan_only_cannot_apply")
    if appmsg_text_max_chars < 1:
        raise RefillError("invalid_appmsg_limit")
    backup = _check_backup(root, backup_dir, apply)
    report: dict[str, Any] = {"mode": "scan" if scan_only else "apply" if apply else "dry_run",
        "scanned_files": 0, "candidate_sessions": 0, "session_days": 0,
        "written_files": 0, "api_requests": 0, "by_type": {}, "files": [], "errors": []}
    sessions: set[str] = set()
    session_days: set[tuple[str, str]] = set()

    def count(label: str, status: str, n: int = 1) -> None:
        report["by_type"].setdefault(label, dict.fromkeys(STATUSES, 0))[status] += n

    # Hold one canonical file and one session-day response at a time. rglob also
    # supports range exports and arbitrary staging roots without filename guesses.
    for path in sorted(root.rglob("*.json")):
        relative = path.relative_to(root).as_posix()
        report["scanned_files"] += 1
        try:
            if not _inside(path.resolve(), root):
                raise RefillError("symlink_outside_raw")
            original = path.read_bytes()
            _, payload = _decode(original)
            if not isinstance(payload, dict) or payload.get("weflow", {}).get("source") != "http_api":
                continue
            messages = payload.get("messages")
            if not isinstance(messages, list):
                continue
            candidates = {i: m for i, m in enumerate(messages) if is_candidate(m)}
            if not candidates:
                continue
            talker = payload.get("session", {}).get("wxid")
            if not isinstance(talker, str) or not talker.strip():
                raise RefillError("missing_session_identity")
            sessions.add(talker)
            outcomes: dict[int, str] = {}
            by_day: dict[date, list[int]] = defaultdict(list)
            changes: dict[int, str] = {}
            key_counts = Counter(_key(m) for m in messages)
            for index, message in candidates.items():
                label = message["content"]
                count(label, "candidate")
                day = datetime.fromtimestamp(_key(message)[1]).date()
                session_days.add((talker, day.isoformat()))
                if key_counts[_key(message)] != 1 or str(message.get("formattedTime", ""))[:10] != day.isoformat():
                    outcomes[index] = "rejected"
                else:
                    by_day[day].append(index)
            if not scan_only:
                for day, indexes in sorted(by_day.items()):
                    try:
                        report["api_requests"] += 1
                        fetched = client.fetch_messages(talker, start=day, end=day, media=False)
                        index_by_key: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
                        for message in fetched:
                            index_by_key[_key(message)].append(message)
                    except Exception:
                        # API exceptions may contain response bodies or credentials.
                        for index in indexes:
                            outcomes[index] = "api_failed"
                        continue
                    for index in indexes:
                        old = candidates[index]
                        matches = index_by_key.get(_key(old), [])
                        if not matches:
                            outcomes[index] = "missing"
                            continue
                        if len(matches) != 1 or not _same_identity(old, matches[0], talker):
                            outcomes[index] = "rejected"
                            continue
                        fresh = matches[0]
                        base, app = unpack_local_type(int(fresh["localType"]))
                        if base not in {48, 49}:
                            # Matching non-appmsg placeholders are not identity
                            # failures and must not block repairs in this file.
                            outcomes[index] = "still_bare"
                            continue
                        resolved = resolve_message_type(int(fresh["localType"]))
                        content = _message_content(fresh, resolved.placeholder, base, app, "", appmsg_text_max_chars)
                        if not content or content in BARE_LABELS:
                            outcomes[index] = "still_bare"
                        else:
                            changes[index] = content
                            outcomes[index] = "refillable"
            # Identity mismatch is a file-level guard, like the byte gate.
            if "rejected" in outcomes.values():
                changes.clear()
                outcomes = {i: "rejected" if s == "refillable" else s for i, s in outcomes.items()}
            error = None
            if changes:
                try:
                    updated = patch_content(original, changes)
                    verify_patch(original, updated, changes)
                    if path.read_bytes() != original:
                        raise RefillError("raw_changed_during_refetch")
                    if apply:
                        assert backup is not None
                        saved = backup / relative
                        saved.parent.mkdir(parents=True, exist_ok=True)
                        if not _inside(saved.resolve(), backup):
                            raise RefillError("backup_path_escaped")
                        # Never replace an earlier backup, including on resume.
                        if saved.exists():
                            if saved.read_bytes() != original:
                                raise RefillError("backup_conflict")
                        else:
                            with saved.open("xb") as stream:
                                stream.write(original)
                                stream.flush()
                                os.fsync(stream.fileno())
                        if saved.read_bytes() != original or path.read_bytes() != original:
                            raise RefillError("backup_or_raw_changed")
                        atomic_replace_bytes(path, updated)
                        report["written_files"] += 1
                        for index in changes:
                            outcomes[index] = "refilled"
                except (ValueError, OSError, KeyError, IndexError, UnicodeError):
                    error = "write_guard_failed"
                    for index in changes:
                        outcomes[index] = "rejected"
            entry: dict[str, Any] = {"path": relative, "candidate": len(candidates), "counts": dict(Counter(outcomes.values()))}
            if error:
                entry["error"] = error
            report["files"].append(entry)
            for index, status in outcomes.items():
                count(candidates[index]["content"], status)
            if progress:
                progress(entry)
        except (ValueError, OSError, TypeError, AttributeError, KeyError, IndexError, UnicodeError, RecursionError):
            report["errors"].append({"path": relative, "error": "invalid_or_unreadable_file"})
    report["candidate_sessions"] = len(sessions)
    report["session_days"] = len(session_days)
    return report


def exit_code(report: dict[str, Any]) -> int:
    if report["errors"] or any(v["rejected"] or v["api_failed"] for v in report["by_type"].values()):
        return 1
    if any(v["missing"] or v["still_bare"] for v in report["by_type"].values()):
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按会话日重取裸标签，只回填 canonical content；默认预览")
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--config")
    parser.add_argument("--scan-only", action="store_true", help="完全离线统计，不读取配置、不请求 API")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", help="apply 必填；与 raw 和公开代码根分离的回退目录")
    args = parser.parse_args(argv)
    try:
        root = Path(args.raw_root).expanduser().resolve()
        backup = _check_backup(root, Path(args.backup_dir) if args.backup_dir else None, args.apply)
        if args.scan_only and args.apply:
            raise RefillError("scan_only_cannot_apply")
        client = None
        limit = 300
        if not args.scan_only:
            cfg = load_config(resolve_config_path(args.config))
            api = cfg.export_backend.weflow_api
            if not api.access_token.strip():
                raise RefillError("nonempty_api_token_required")
            client = WeflowApiClient(api.base_url, api.access_token,
                timeout=api.request_timeout_sec, message_timeout=api.message_request_timeout_sec)
            limit = api.appmsg_text_max_chars
        def progress(entry: dict[str, Any]) -> None:
            print(json.dumps(entry, ensure_ascii=True), flush=True)
        report = refill(root, client=client, apply=args.apply, backup_dir=backup,
                        scan_only=args.scan_only, appmsg_text_max_chars=limit, progress=progress)
    except RefillError as exc:
        hints = {
            "apply_requires_backup_dir": "写入必须提供 --backup-dir <分离回退目录>。",
            "backup_must_be_separate_from_raw_and_code": "--backup-dir 必须在代码根之外，且与 raw 根互不包含。",
            "scan_only_cannot_apply": "--scan-only 不能与 --apply 同时使用。",
            "raw_root_not_directory": "--raw-root 不存在或不是目录。",
            "nonempty_api_token_required": "请在工作区配置中填写固定非空 API access_token。",
        }
        print("appmsg 回填未启动：" + hints.get(str(exc), "参数或配置校验失败。"), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"appmsg 回填未启动：{type(exc).__name__}；检查路径、配置与参数。", file=sys.stderr)
        return 2
    # File entries were streamed already. Do not duplicate a potentially huge
    # list on the summary line (which should remain usable with tail -1).
    summary = {k: v for k, v in report.items() if k not in {"files", "errors"}}
    summary["candidate_files"] = len(report["files"])
    summary["error_files"] = len(report["errors"])
    for error in report["errors"]:
        print(json.dumps(error, ensure_ascii=True), flush=True)
    print(json.dumps(summary, ensure_ascii=True), flush=True)
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
