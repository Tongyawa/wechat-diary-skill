"""Read-only archive plans and byte-preserving execution of those exact plans.

Only direct, single-day chat JSON has a (wxid, day) identity. Generic tree
merging deliberately lives elsewhere. No constructor or planning path writes.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid

from .archiving import strip_date_suffix
from .chat_identity import (
    _collision_failure_reason, _is_chat_json, _read_stored_session,
    _chat_regression_reason, compare_chat_identities,
)
from .media_store import MEDIA_STORE_NAME, is_media_reference
from .moments_ranges import range_digest, plan_range_publication
from .preprocessing.image_ocr import _looks_like_image_ref
from .raw_schema import RawSchemaError, validate_moments_json, validate_session_json

DAY_PATTERN = re.compile(r"_(\d{8})\.json$", re.IGNORECASE)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprint(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        return "<not-file>"
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _day(path: Path) -> str | None:
    match = DAY_PATTERN.search(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match[1], "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def reject_overlapping_roots(archive: Path, sources: list[Path]) -> None:
    archive = archive.resolve()
    for source in sources:
        source = source.resolve()
        if source == archive or source.is_relative_to(archive) or archive.is_relative_to(source):
            raise ValueError("摄取源根与 archived 根不得相同、包含或被包含")


def _inventory(root: Path, *, file_ids: dict | None = None) -> dict[str, tuple[int, int]]:
    result = {}
    if root.is_dir():
        for directory in root.iterdir():
            if directory.is_dir() and directory.name != MEDIA_STORE_NAME:
                for path in directory.glob("*.json"):
                    info = path.stat()
                    relative = path.relative_to(root).as_posix()
                    result[relative] = (info.st_size, info.st_mtime_ns)
                    if file_ids is not None:
                        file_ids[relative] = (info.st_dev, info.st_ino, info.st_ctime_ns)
    return result


@dataclass
class ArchiveIndex:
    root: Path
    by_key: dict = field(default_factory=lambda: defaultdict(list))
    directories: dict = field(default_factory=lambda: defaultdict(set))
    members: dict = field(default_factory=lambda: defaultdict(set))
    unknown: set = field(default_factory=set)
    issues: list = field(default_factory=list)
    inventory: dict = field(default_factory=dict)
    file_ids: dict = field(default_factory=dict)
    elapsed: float = 0.0

    def directory_issue(self, name: str, wxid: str) -> str | None:
        if name in self.unknown:
            return "directory_identity_unknown"
        if self.members.get(name, set()) - {wxid}:
            return "directory_multiple_wxids"
        return None


def build_archive_index(archive_raw_root: Path | str, *, header_cache: dict | None = None) -> ArchiveIndex:
    started = time.perf_counter()
    index = ArchiveIndex(Path(archive_raw_root).resolve())
    if header_cache and header_cache.get("root") != str(index.root):
        raise ValueError("header cache belongs to another archive")
    index.inventory = _inventory(index.root, file_ids=index.file_ids)
    for relative in sorted(index.inventory):
        path = index.root / relative
        directory = path.parent.name
        cached = (header_cache or {}).get("files", {}).get(relative)
        signature = (*index.inventory[relative], *index.file_ids[relative])
        session = (cached["session"] if cached and tuple(cached["signature"]) == signature
                   else _read_stored_session(path))
        wxid = str((session or {}).get("wxid") or "").strip()
        day = _day(path)
        if wxid:
            index.members[directory].add(wxid)
            index.directories[wxid].add(directory)
        else:
            index.unknown.add(directory)
        if not wxid or not day:
            index.issues.append({"path": relative, "reason": "unknown_wxid" if not wxid else "unsupported_day"})
        else:
            index.by_key[wxid, day].append(path)
    index.elapsed = time.perf_counter() - started
    return index


@dataclass
class RawAction:
    source: Path
    source_root: int
    relative: str
    target: Path
    kind: str
    source_sha: str
    target_before: str | None
    wxid: str = ""
    day: str | None = None
    resolution: str = "path"
    status: str = "ready"
    reasons: list[str] = field(default_factory=list)
    existing: list[dict] = field(default_factory=list)
    guards: list[dict] = field(default_factory=list)
    retire: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    group: str = ""
    duplicate: bool = False
    noop: bool = False
    retain_source: bool = False
    depends_on: Path | None = None
    cas_references: set[Path] = field(default_factory=set, repr=False)
    report_context: dict = field(default_factory=dict, repr=False)
    range_review: bool = False
    projected_objects: dict[Path, tuple[Path | None, str | None]] = field(default_factory=dict, repr=False)
    ordered_after: tuple[Path, ...] = ()

    def block(self, reason: str, status: str = "resolutionBlocked") -> None:
        self.status = status
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.retire.clear()

    def to_dict(self, root: Path) -> dict:
        result = {
            "sourceRoot": self.source_root, "source": self.relative,
            "sourceSha256": self.source_sha, "identity": {"wxid": self.wxid, "day": self.day},
            "target": self.target.relative_to(root).as_posix(), "targetBeforeSha256": self.target_before,
            "resolution": self.resolution, "status": self.status, "reasons": self.reasons,
            "existingFiles": self.existing, "guard": self.guards,
            "retire": [p.relative_to(root).as_posix() for p in self.retire],
            "warnings": self.warnings,
            "retainSource": self.retain_source,
            "dependsOnSource": str(self.depends_on) if self.depends_on else None,
        }
        if self.projected_objects:
            result['projectedObjects'] = [{
                'path': path.relative_to(root).as_posix(),
                'producedBySource': str(source) if source else None, 'sha256': sha}
                for path, (source, sha) in sorted(self.projected_objects.items())]
        if self.ordered_after:
            result['orderedAfterSources'] = [str(p) for p in self.ordered_after]
        return result


@dataclass
class RawIngestPlan:
    archive: Path
    roots: list[Path]
    policy: str
    index: ArchiveIndex
    actions: list[RawAction] = field(default_factory=list)
    schema_failures: list[dict] = field(default_factory=list)
    hypothetical: bool = False
    retained_directories: list[dict] = field(default_factory=list)
    checked_actions: dict = field(default_factory=dict, repr=False)
    range_initial: dict | None = field(default=None, repr=False)
    range_evidence: dict = field(default_factory=dict, repr=False)
    media_objects: dict = field(default_factory=dict, repr=False)
    checked_context: tuple = field(default_factory=tuple, repr=False)

    @property
    def summary(self) -> dict:
        counts = Counter(a.status for a in self.actions)
        return {
            "ready": counts["ready"], "resolutionBlocked": counts["resolutionBlocked"],
            "hardBlocked": counts["hardBlocked"], "schemaInvalid": len(self.schema_failures),
            "coverageFailed": sum(not old["coverage"]["covered"] for a in self.actions
                                  for old in a.existing if old.get("coverageRequired")),
            "forceEligible": sum(g["forceEligible"] for a in self.actions for g in a.guards),
            "chatFiles": sum(a.kind == "chat" for a in self.actions),
            "fallback": sum(a.resolution == "legacy_fallback" for a in self.actions),
        }

    def to_dict(self) -> dict:
        result = {
            "schemaVersion": 1, "policy": self.policy, "hypothetical": self.hypothetical,
            "roots": {"archive": str(self.archive), "incoming": [str(p) for p in self.roots]},
            "index": {"complete": not self.index.issues, "issues": self.index.issues,
                      "files": len(self.index.inventory)},
            "chatItems": [a.to_dict(self.archive) for a in self.actions if a.kind == "chat"],
            "nonChatActions": [dict(a.to_dict(self.archive), kind=a.kind)
                               for a in self.actions if a.kind != "chat"],
            "schemaFailures": self.schema_failures, "summary": self.summary,
            "archiveInventory": self.index.inventory,
            "retainedDirectories": self.retained_directories,
        }
        if self.range_initial is not None:
            result['rangeInitialFiles'] = {p.relative_to(self.archive).as_posix(): sha
                                           for p, sha in sorted(self.range_initial.items())}
        result["planId"] = _sha(json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        return result


def _legacy_target(root: Path, relative: Path) -> Path:
    if len(relative.parts) == 1:
        return root / relative
    first = relative.parts[0]
    directory = first if first == MEDIA_STORE_NAME else strip_date_suffix(first)
    return root / directory / Path(*relative.parts[1:])


def _chat_description(path: Path, payload=None) -> dict:
    """Diagnostic identity/count, never a coverage proof or a claimed zero count."""
    if payload is None and path.is_file() and path.suffix.lower() == '.json':
        try:
            payload = json.loads(path.read_bytes().decode('utf-8-sig'))
        except (OSError, ValueError):
            pass
    payload = payload if isinstance(payload, dict) else {}
    session = payload.get('session')
    session = session if isinstance(session, dict) else (
        (_read_stored_session(path) or {}) if path.suffix.lower() == '.json' else {})
    messages = payload.get('messages')
    return {'path': str(path), 'identity': {'wxid': str(session.get('wxid') or ''), 'day': _day(path)},
            'displayName': str(session.get('displayName') or session.get('nickname') or ''),
            'messageCount': len(messages) if isinstance(messages, list) else None,
            'exists': path.is_file()}


def _daily_fallback(plan: RawIngestPlan, action: RawAction, reason: str, incoming: dict) -> None:
    """Choose the historical route; only final target inspection permits writing."""
    action.target = _legacy_target(plan.archive, Path(action.relative))
    action.target_before = _fingerprint(action.target)
    action.resolution = "legacy_fallback"
    action.status = "ready"
    action.reasons.clear()
    action.existing.clear()
    action.guards.clear()
    action.retire.clear()
    if reason not in action.warnings:
        action.warnings.append(reason)
    action.report_context['legacyTarget'] = _chat_description(action.target)


def _json_prefix_for_media(raw: bytes) -> dict:
    """Recover complete fields before truncation, solely for retaining media.

    This is never accepted as a valid chat, a message count or a coverage proof.
    Stop at the first broken token; do not search names/quoted text for paths.
    """
    text = raw.decode('utf-8-sig', errors='replace')
    decoder = json.JSONDecoder()

    def whitespace(position):
        return re.compile(r'\s*').match(text, position).end()

    def object_prefix(position, *, top=False):
        fields = {}
        position = whitespace(position)
        if not text.startswith('{', position):
            return fields
        position = whitespace(position + 1)
        while position < len(text):
            try:
                key, position = decoder.raw_decode(text, position)
                position = whitespace(position)
                if not isinstance(key, str) or not text.startswith(':', position):
                    break
                position = whitespace(position + 1)
                try:
                    value, position = decoder.raw_decode(text, position)
                except ValueError:
                    if (top and key in {'messages', 'posts'} or key == 'media') and text.startswith('[', position):
                        fields[key] = messages_prefix(position + 1)
                    elif key == 'livePhoto' and text.startswith('{', position):
                        fields[key] = object_prefix(position)
                    break
                fields[key] = value
                position = whitespace(position)
                if not text.startswith(',', position):
                    break
                position = whitespace(position + 1)
            except ValueError:
                break
        return fields

    def messages_prefix(position):
        messages = []
        position = whitespace(position)
        while position < len(text):
            try:
                message, end = decoder.raw_decode(text, position)
            except ValueError:
                partial = object_prefix(position)
                if partial:
                    messages.append(partial)
                break
            messages.append(message)
            position = whitespace(end)
            if not text.startswith(',', position):
                break
            position = whitespace(position + 1)
        return messages

    return object_prefix(0, top=True)


def _chat_cas_references(payload: dict, source: Path, root: Path) -> set[Path]:
    references = set()
    store = (root / MEDIA_STORE_NAME).resolve()
    # Raw media dependencies are source fields and the content image references
    # actually opened by preprocessing, never names, quotations or transcripts.
    messages = payload.get("messages")
    values = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        values.append(message.get("source"))
        content = message.get("content")
        if isinstance(content, str) and _looks_like_image_ref(content):
            values.append(content)
    # Moments references include the canonical Live Photo companion. Metadata,
    # captions and URLs do not create dependencies; chat fields stay unchanged.
    posts = payload.get('posts')
    for post in posts if isinstance(posts, list) else []:
        media = post.get('media') if isinstance(post, dict) else None
        for item in media if isinstance(media, list) else []:
            if isinstance(item, dict):
                values.append(item.get('localPath'))
                live_photo = item.get('livePhoto')
                if isinstance(live_photo, dict):
                    values.append(live_photo.get('localPath'))
    for value in values:
        if not is_media_reference(value):
            continue
        path = (source.parent / value.strip().replace("\\", "/")).resolve()
        if path.is_relative_to(store):
            references.add(path)
    return references


def _resolve(index: ArchiveIndex, action: RawAction, source_name: str, *, daily: bool = False) -> tuple[str | None, str]:
    if not action.wxid or not action.day or len(Path(action.relative).parts) != 2:
        return "unsupported_chat_shape", ""
    directories = index.directories.get(action.wxid, set())
    # Daily names follow the host filesystem. Bulk retains its existing strict
    # name gate, including rejection of case-only matches in a split identity.
    if daily:
        source_name = next((name for name in set(index.members) | index.unknown
                            if os.path.normcase(name) == os.path.normcase(source_name)), source_name)
    if len(directories) > 1:
        if source_name not in directories:
            return "multiple_directories_without_name_match", ""
        directory = source_name
    elif directories:
        directory = next(iter(directories))
    else:
        directory = source_name
    for name in {directory, *(p.parent.name for p in index.by_key.get((action.wxid, action.day), []))}:
        reason = index.directory_issue(name, action.wxid)
        if reason:
            return reason, ""
    action.resolution = ("existing_day" if (action.wxid, action.day) in index.by_key
                         else "existing_session" if directories else "new_session")
    return None, directory


def _old_record(plan: RawIngestPlan, path: Path, incoming: dict, *, required: bool,
                incoming_sha: str) -> tuple[dict, dict]:
    raw = path.read_bytes()
    old = json.loads(raw.decode("utf-8-sig"))
    if not _is_chat_json(old):
        raise ValueError("archive_chat_invalid")
    if plan.policy != "rename":
        validate_session_json(old)
    record = {"path": path.relative_to(plan.archive).as_posix(), "sha256": _sha(raw),
              "action": "replace", "coverageRequired": required,
              "coverage": compare_chat_identities(incoming, old).to_dict()}
    if not record['coverage']['covered'] and record['sha256'] == incoming_sha:
        # Exact bytes need no generation inference. Keep this proof distinct
        # from identity comparison, and keep all different-byte guards intact.
        record['coverage'] = {'covered': True, 'basis': 'identical_sha256'}
    return record, old


def plan_raw_ingest(
    archive_raw_root: Path | str, source_roots: list[Path | str], *, policy: str = "bulk",
    index: ArchiveIndex | None = None, invalid_paths: set[Path] | None = None,
    incoming_overrides: dict[Path, bytes] | None = None,
    only_paths: set[Path] | None = None, rename_targets: dict[str, str] | None = None,
    header_cache: dict | None = None,
) -> RawIngestPlan:
    """Make one complete plan. Overrides are analysis-only and cannot be executed."""
    archive = Path(archive_raw_root).resolve()
    roots = [Path(p).resolve() for p in source_roots]
    if policy not in {"bulk", "daily", "rename"}:
        raise ValueError("unknown archive policy")
    if policy != "rename":
        reject_overlapping_roots(archive.parent, roots)
    if any(not p.is_dir() for p in roots):
        raise ValueError("raw source root is not a directory")
    if len(set(roots)) != len(roots):
        raise ValueError("duplicate source root")
    active_index = index or build_archive_index(archive, header_cache=header_cache)
    if active_index.root != archive:
        raise ValueError("index belongs to another archive")
    plan = RawIngestPlan(archive, roots, policy, active_index, hypothetical=bool(incoming_overrides))
    invalid = {p.resolve() for p in (invalid_paths or set())}
    overrides = {p.resolve(): value for p, value in (incoming_overrides or {}).items()}
    selected = {p.resolve() for p in only_paths} if only_paths is not None else None
    groups: dict[str, list[RawAction]] = defaultdict(list)
    keys: dict[tuple[str, str], list[RawAction]] = defaultdict(list)
    daily_payloads = {}
    for root_number, source_root in enumerate(roots):
        candidates = (p for p in selected if p.is_relative_to(source_root)) if selected is not None else source_root.rglob("*")
        for source in sorted(p for p in candidates if p.is_file()):
            if selected is not None and source not in selected:
                continue
            relative = source.relative_to(source_root)
            if source in invalid:
                plan.schema_failures.append({"sourceRoot": root_number, "path": relative.as_posix(),
                                             "reason": "raw_schema_invalid"})
                continue
            is_media = relative.parts[0] == MEDIA_STORE_NAME or "media" in relative.parts[:-1]
            raw = overrides.get(source)
            if raw is None and source.suffix.lower() == ".json" and not is_media:
                raw = source.read_bytes()
            source_sha = _sha(raw) if raw is not None else _fingerprint(source)
            payload = None
            if source.suffix.lower() == ".json" and not is_media:
                try:
                    payload = json.loads(raw.decode("utf-8-sig"))
                    if policy == "bulk":
                        if _is_chat_json(payload):
                            validate_session_json(payload)
                        elif isinstance(payload, dict) and isinstance(payload.get("posts"), list):
                            validate_moments_json(payload)
                        else:
                            raise ValueError("not a canonical raw JSON")
                except (ValueError, UnicodeError, RawSchemaError) as exc:
                    if policy == "bulk":
                        plan.schema_failures.append({"sourceRoot": root_number, "path": relative.as_posix(),
                                                     "reason": str(exc)})
                        continue
            chat = _is_chat_json(payload)
            # A partial session object is still a chat-shaped file, never a safe opaque copy.
            partial_chat = isinstance(payload, dict) and isinstance(payload.get("session"), dict)
            target = _legacy_target(archive, relative)
            group = f"{root_number}:{relative.parts[0]}" if len(relative.parts) > 1 else ""
            action = RawAction(source, root_number, relative.as_posix(), target,
                               "chat" if chat or partial_chat else "range" if len(relative.parts) == 1 and range_digest(source) else "file",
                               source_sha, _fingerprint(target), group=group)
            plan.actions.append(action)
            if group:
                groups[group].append(action)
            if policy == 'daily' and not is_media and len(relative.parts) == 2 and _day(source):
                try:
                    validate_session_json(payload)
                except (ValueError, TypeError, RawSchemaError):
                    if target.is_file():
                        # A chat-shaped pathname cannot replace an existing
                        # archive object with truncated or noncanonical input.
                        action.kind = 'chat'
                        action.day = _day(source)
                        action.wxid = str((_read_stored_session(source) or {}).get('wxid') or '')
                        action.report_context = {'incoming': _chat_description(source, payload),
                                                 'legacyTarget': _chat_description(target)}
                        media_payload = payload if isinstance(payload, dict) else _json_prefix_for_media(raw)
                        action.cas_references = _chat_cas_references(media_payload, source, source_root)
                        daily_payloads[source] = payload
                        action.warnings.append('invalid_incoming_chat')
                        continue
            if action.kind != "chat":
                continue
            action.wxid = str(payload["session"].get("wxid") or "").strip()
            action.day = _day(source)
            action.cas_references = _chat_cas_references(payload, source, source_root)
            if policy == 'daily':
                daily_payloads[source] = payload
                action.report_context['incoming'] = _chat_description(source, payload)
            if action.wxid and action.day:
                keys[action.wxid, action.day].append(action)
            source_name = strip_date_suffix(relative.parts[0]) if len(relative.parts) > 1 else ""
            reason, directory = _resolve(active_index, action, source_name, daily=policy == 'daily')
            if not chat:
                reason = "unsupported_chat_payload"
            if policy == "rename":
                directory = (rename_targets or {}).get(relative.parts[0], "")
                reason = ("unsupported_chat_shape" if not chat or not action.day or len(relative.parts) != 2
                          else active_index.directory_issue(relative.parts[0], action.wxid)
                          or active_index.directory_issue(directory, action.wxid))
                if not directory:
                    reason = "rename_target_unknown"
            if reason:
                if policy == "daily":
                    _daily_fallback(plan, action, reason, payload)
                else:
                    action.block(reason)
                continue
            action.target = archive / directory / (source.name if policy == "rename"
                                                   else f"{directory}_{action.day.replace('-', '')}.json")
            action.target_before = _fingerprint(action.target)
            siblings = list(active_index.by_key.get((action.wxid, action.day), []))
            if policy == "rename":
                action.resolution = "rename"
                siblings = [p for p in siblings if p.parent.name == directory and p != source]
                identical = next((p for p in siblings if _fingerprint(p) == action.source_sha), None)
                predecessor = next((a for a in plan.actions if a is not action and a.kind == "chat"
                                    and a.status == "ready" and a.wxid == action.wxid and a.day == action.day
                                    and a.target.parent.name == directory and a.source_sha == action.source_sha), None)
                if identical is None and predecessor is not None:
                    identical = predecessor.target
                    action.depends_on = predecessor.source
                if identical:
                    action.target = identical
                    action.target_before = _fingerprint(identical)
                    record, _ = _old_record(plan, source, payload, required=False,
                                            incoming_sha=action.source_sha)
                    record["retirementBasis"] = "identical_sha256"
                    record["action"] = "backup_then_retire"
                    action.existing.append(record)
                    action.retire.append(source)
                    action.duplicate = True
                    continue
                # Only same-path collisions keep the existing rename guard.
                siblings = [action.target] if action.target.is_file() else []
            legacy_target = _legacy_target(archive, relative)
            for old_path in siblings:
                try:
                    required = True
                    record, old = _old_record(plan, old_path, payload, required=required,
                                              incoming_sha=action.source_sha)
                    record["action"] = "replace" if old_path == action.target else "backup_then_retire"
                    action.existing.append(record)
                    if required and not record["coverage"]["covered"]:
                        if policy == "daily":
                            _daily_fallback(plan, action, "coverage_failed_fallback:" + record["path"], payload)
                            break
                        else:
                            action.block("coverage_failed", "hardBlocked")
                    elif old_path != action.target:
                        action.retire.append(old_path)
                    if policy != "daily":
                        decision = None if raw == old_path.read_bytes() else _chat_regression_reason(payload, old)
                        if decision:
                            reason_text, force = decision
                            action.guards.append({"path": record["path"], "reason": reason_text,
                                                  "forceEligible": force})
                            if not force or policy == "rename":
                                action.block("overwrite_guard", "hardBlocked")
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    reason = "archive_unreadable:" + type(exc).__name__
                    if policy == "daily":
                        _daily_fallback(plan, action, reason, payload)
                        break
                    action.block(reason, "hardBlocked")
            if action.resolution == "legacy_fallback":
                continue
            if action.target_before == "<not-file>":
                action.block("target_not_file")
            elif action.target.is_file() and not any(archive / e["path"] == action.target for e in action.existing):
                # Filename collision with an object of another identity is never silently replaced.
                action.block("target_not_in_identity_index")

    if policy != "rename":
        for actions in keys.values():
            if len(actions) > 1:
                for action in actions:
                    action.block("multiple_incoming_for_day")
    # Settle every chat fallback before routing its session-local attachments.
    directory_members = defaultdict(set)
    for action in plan.actions:
        if action.kind == "chat" and action.wxid and action.status == "ready":
            directory_members[action.target.parent].add(action.wxid)
    for action in plan.actions:
        if action.kind == "chat" and action.status == "ready" and len(directory_members[action.target.parent]) > 1:
            if policy == "daily":
                _daily_fallback(plan, action, "planned_directory_multiple_wxids", daily_payloads[action.source])
            else:
                action.block("planned_directory_multiple_wxids")
    for actions in groups.values():
        chats = [a for a in actions if a.kind == "chat"]
        destinations = {a.target.parent for a in chats if a.status == "ready"}
        blocked = any(a.status == "resolutionBlocked" for a in chats)
        if len({a.wxid for a in chats if a.wxid}) > 1 and policy != "daily":
            blocked = True
            for action in chats:
                action.block("incoming_directory_multiple_wxids")
        if len(destinations) > 1:
            blocked = True
            for action in chats:
                action.block("session_directory_targets_disagree")
        if blocked:
            for action in actions:
                if action.status == "ready":
                    action.block("session_directory_blocked")
        elif destinations:
            directory = next(iter(destinations))
            for action in actions:
                if action.kind != "chat":
                    action.target = directory / Path(*Path(action.relative).parts[1:])
                    action.target_before = _fingerprint(action.target)
    # Non-chat collisions are part of this plan, including legacy attachments.
    for action in plan.actions:
        if action.kind == "chat" or action.kind == "range" or action.status != "ready":
            continue
        if action.target_before == "<not-file>":
            action.block("target_not_file")
        elif action.target_before and action.target_before != action.source_sha:
            legacy_media = "media" in Path(action.relative).parts[:-1] and any(
                a.kind == "chat" for a in groups.get(action.group, []))
            cas_with_ranges = Path(action.relative).parts[0] == MEDIA_STORE_NAME and any(
                a.kind == "range" for a in plan.actions)
            if policy != "daily" or legacy_media or cas_with_ranges:
                decision = _collision_failure_reason(action.source, action.target)
                if decision:
                    reason, force = decision
                    force = force and not legacy_media and not cas_with_ranges
                    action.guards.append({"path": action.target.relative_to(archive).as_posix(),
                                          "reason": reason, "forceEligible": force})
                    if not force or policy == "rename":
                        action.block("overwrite_guard", "hardBlocked")
    # A blocked attachment must not leave a newly published JSON pointing at wrong bytes.
    for actions in groups.values():
        if any(a.status != "ready" and a.kind != "chat" for a in actions):
            for action in actions:
                if action.status == "ready":
                    action.block("session_directory_blocked")
    destinations = defaultdict(list)
    for action in plan.actions:
        if action.status == "ready":
            destinations[action.target].append(action)
    for actions in destinations.values():
        if len({a.source_sha for a in actions}) > 1:
            for action in actions:
                action.block("multiple_incoming_for_target", "hardBlocked")
    # Range destinations and retirements are resolved once, before the same
    # final inspection as every other action. Execution never re-enumerates.
    range_actions = [a for a in plan.actions if a.status == 'ready' and a.kind == 'range']
    virtual = {}
    prior_ranges = defaultdict(list)
    if range_actions:
        plan.range_initial = _range_inventory(archive)
        virtual = {p: p for p in plan.range_initial}
        plan.media_objects = {a.target: a.source for a in plan.actions if a.status == 'ready' and a.kind == 'file'}
    for action in range_actions:
        if action.status == 'ready' and action.kind == 'range':
            try:
                publication = plan_range_publication(action.source, action.target,
                    new_media_root=archive, objects=virtual, media_objects=plan.media_objects)
                action.target = publication.target
                action.target_before = _fingerprint(action.target)
                digest = range_digest(action.target)
                action.ordered_after = tuple(prior_ranges[digest])
                for path in [action.target, *(p for p, _ in publication.retire)]:
                    evidence = virtual.get(path)
                    if evidence != path:
                        action.projected_objects[path] = (evidence, _fingerprint(evidence) if evidence else None)
                action.range_review = bool(publication.preserved)
                action.cas_references.update(publication.media_paths)
                plan.range_evidence.update({p: _fingerprint(p) for p in publication.media_paths})
                action.warnings.extend('range_retained:' + p.name for p, _ in publication.preserved)
                for path, coverage in publication.retire:
                    action.retire.append(path)
                    action.existing.append({'path': path.relative_to(archive).as_posix(),
                        'sha256': _fingerprint(virtual[path]), 'action': 'backup_then_retire',
                        'coverageRequired': False, 'backupRequired': True,
                        'retirementBasis': 'moments_coverage',
                        'coverage': {'covered': coverage.covered, 'basis': 'posts_and_media'}})
                for path in action.retire:
                    virtual.pop(path)
                # Windows aliases compare equal, but the new publication's
                # spelling becomes the name seen by later sibling scans.
                virtual.pop(action.target, None)
                virtual[action.target] = action.source
                prior_ranges[digest].append(action.source)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                action.block('range_unverifiable:' + type(exc).__name__, 'hardBlocked')
    # Archive-internal moves also remove an existing object: make that removal
    # visible to final inspection and backup, instead of hiding it in cleanup.
    if policy == 'rename':
        for action in plan.actions:
            if action.status != 'ready' or action.source == action.target or action.source in action.retire:
                continue
            if action.kind == 'chat':
                record, _ = _old_record(plan, action.source,
                    json.loads(action.source.read_bytes().decode('utf-8-sig')), required=True,
                    incoming_sha=action.source_sha)
            else:
                record = {'path': action.source.relative_to(archive).as_posix(),
                          'sha256': action.source_sha, 'coverageRequired': False,
                          'coverage': {'covered': False, 'basis': 'byte_backup'}}
            record.update(action='backup_then_retire', backupRequired=True, retirementBasis='identical_sha256')
            action.existing.append(record)
            action.retire.append(action.source)
    # Unordered mutations cannot share an object. Ordered range actions instead
    # bind each intermediate version to its producing source and inspected SHA.
    mutations = defaultdict(list)
    for action in plan.actions:
        if action.status == 'ready':
            for path in [action.target, *action.retire]:
                mutations[path].append(action)
    for path, actions in mutations.items():
        unique = {id(a): a for a in actions}.values()
        ordered = all(a.source in b.ordered_after for i, a in enumerate(list(unique))
                      for b in list(unique)[i + 1:])
        if not ordered and len({id(a) for a in actions}) > 1 and (any(path in a.retire for a in actions)
                or len({a.source_sha for a in actions}) > 1):
            for action in unique:
                action.block('overlapping_archive_mutations', 'hardBlocked')
    for action in plan.actions:
        if action.status == 'ready':
            _final_target_check(plan, action, incoming_bytes=overrides.get(action.source))
    finalize_dependencies(plan)
    # Dependency retention can only remove a planned cleanup, never introduce
    # a new destination. Keep shared rename media in its old directory too.
    for action in plan.actions:
        if action.retain_source and action.source in action.retire:
            action.retire.remove(action.source)
            for old in action.existing:
                if archive / old['path'] == action.source:
                    old['action'] = 'keep'
    plan.checked_actions = {a.source: _execution_signature(a) for a in plan.actions if a.status == 'ready'}
    plan.checked_context = _context_signature(plan)
    removed = _planned_archive_removals(plan)
    plan.retained_directories = _directory_retention(plan, _old_directories(plan), removed)
    return plan


def _execution_signature(action: RawAction) -> tuple:
    """Bind execution to the inspected paths, bytes, and backup obligations."""
    return (action.source, action.target, action.source_sha, action.target_before,
            tuple(action.retire), json.dumps(action.existing, sort_keys=True), action.duplicate, action.noop,
            json.dumps(action.guards, sort_keys=True), action.range_review,
            tuple(sorted(action.projected_objects.items())), action.ordered_after,
            action.kind, action.group, action.depends_on, tuple(sorted(action.cas_references)))


def _range_inventory(archive: Path) -> dict:
    # Keep the whole root file namespace: an existing case alias may fail the
    # range-name regex while still occupying a valid incoming destination.
    return {p: _fingerprint(p) for p in archive.iterdir() if p.is_file()} if archive.is_dir() else {}


def _context_signature(plan: RawIngestPlan) -> tuple:
    return (plan.archive, tuple(plan.roots), plan.policy, plan.hypothetical, tuple(a.source for a in plan.actions),
            None if plan.range_initial is None else tuple(sorted(plan.range_initial.items())),
            tuple(sorted(plan.range_evidence.items())), tuple(sorted(plan.media_objects.items())))


def _final_target_check(plan: RawIngestPlan, action: RawAction, *, incoming_bytes: bytes | None = None) -> None:
    """Inspect final filesystem objects, independent of kind, routing or group.

    All routing is already settled. This function never changes a target or
    selects a fallback; a refusal retains that input through normal dependencies.
    """
    def evidence(path):
        return action.projected_objects.get(path, (path, None))[0]

    def decode(path, data=None):
        if path.suffix.lower() != '.json':
            return None, False
        try:
            provider = evidence(path)
            if data is None and provider is None:
                return None, False
            payload = json.loads((data if data is not None else provider.read_bytes()).decode('utf-8-sig'))
            validate_session_json(payload)
            return payload, bool(str(payload['session'].get('wxid') or '').strip())
        except (OSError, ValueError, TypeError):
            return locals().get('payload'), False

    def chat_shape(path, payload):
        return (isinstance(payload, dict) and isinstance(payload.get('session'), dict)
                or bool(evidence(path) and _read_stored_session(evidence(path))) if path.suffix.lower() == '.json' else False)

    def identity(path, payload):
        session = payload.get('session') if isinstance(payload, dict) else None
        session = session if isinstance(session, dict) else (
            (_read_stored_session(evidence(path)) or {}) if evidence(path) else {})
        return str(session.get('wxid') or '').strip(), _day(path)

    def refuse(reason):
        action.report_context['incoming'] = _chat_description(action.source, incoming)
        action.report_context['finalTarget'] = _chat_description(action.target)
        # Media recovery is diagnostic only; never a valid incoming or proof.
        media = incoming if isinstance(incoming, dict) else (
            _json_prefix_for_media(incoming_bytes if incoming_bytes is not None else action.source.read_bytes())
            if action.source.suffix.lower() == '.json' else {})
        action.cas_references.update(_chat_cas_references(media, action.source, plan.roots[action.source_root]))
        action.block(reason, 'hardBlocked')

    # The resolver already hashed this exact final path. JSON proofs must bind
    # the bytes we inspect now to that hash. Opaque media need no second full
    # read during planning; validate_plan rehashes them before any execution.
    target_bytes = None
    if action.target_before == '<not-file>':
        action.block('target_not_file', 'hardBlocked')
        return
    expected_target = action.projected_objects.get(action.target, (action.target, action.target_before))[1]
    target_sha = expected_target
    if target_sha not in (None, '<not-file>') and action.target.suffix.lower() == '.json':
        target_bytes = evidence(action.target).read_bytes()
        target_sha = _sha(target_bytes)
    if target_sha != expected_target:
        action.block('target_changed_during_planning', 'hardBlocked')
        return
    if target_sha == '<not-file>':
        action.block('target_not_file', 'hardBlocked')
        return
    if target_sha is None and not action.retire:
        return
    incoming, valid_incoming = decode(action.source, incoming_bytes)
    incoming_identity = identity(action.source, incoming) if valid_incoming else ('', None)
    records = {plan.archive / old['path']: old for old in action.existing}
    for path in action.retire:
        old_record = records.get(path)
        old, valid_old = decode(path)
        provider = evidence(path)
        old_sha = _fingerprint(provider) if provider else None
        if not old_record or old_sha is None or old_sha == '<not-file>' or old_record['sha256'] != old_sha:
            refuse('retire_without_existing_record')
            return
        if valid_old or chat_shape(path, old):
            if (valid_old and valid_incoming and incoming_identity[1]
                    and identity(path, old) == incoming_identity and old_sha == action.source_sha
                    and not old_record.get('coverage', {}).get('covered')):
                # Exact bytes prove coverage even when generation metadata or
                # platform IDs are absent. Do not claim an identity comparison.
                old_record['coverage'] = {'covered': True, 'basis': 'identical_sha256'}
            if (not valid_old or not valid_incoming or not incoming_identity[1]
                    or identity(path, old) != incoming_identity
                    or not old_record.get('coverage', {}).get('covered')
                    or not (old_sha == action.source_sha or compare_chat_identities(incoming, old).covered)):
                refuse('retire_not_covered')
                return
        if not valid_old:
            old_record['backupRequired'] = True
        old_record['action'] = 'backup_then_retire'
    if target_sha is None:
        return
    if target_sha == action.source_sha:
        action.noop = True
        if action.target in records and plan.policy == 'daily':
            records[action.target]['action'] = 'keep'
        return
    old, valid_old = decode(action.target, target_bytes)
    looks_chat = valid_old or chat_shape(action.target, old)
    # A malformed direct dated JSON without an identifiable header cannot be
    # assumed to be a replaceable opaque attachment.
    parts = action.target.relative_to(plan.archive).parts
    looks_chat |= bool(len(parts) == 2 and _day(action.target) and parts[0] != MEDIA_STORE_NAME)
    if looks_chat:
        old_identity = identity(action.target, old)
        if not valid_incoming:
            refuse('invalid_incoming_chat')
            return
        if not old_identity[0] or not old_identity[1]:
            refuse('final_target_identity_unknown')
            return
        if incoming_identity != old_identity:
            refuse('final_target_other_identity')
            return
        if not valid_old:
            if plan.policy != 'daily':
                refuse('final_target_unreadable')
                return
            record = {'path': action.target.relative_to(plan.archive).as_posix(),
                      'sha256': target_sha, 'action': 'replace', 'coverageRequired': False,
                      'backupRequired': True, 'backupBasis': 'archive_unreadable',
                      'coverage': {'covered': False, 'reason': 'archive_unreadable'}}
        else:
            coverage = compare_chat_identities(incoming, old).to_dict()
            if not coverage['covered']:
                refuse('final_target_not_covered')
                return
            record = records.get(action.target) or {
                'path': action.target.relative_to(plan.archive).as_posix(), 'sha256': target_sha,
                'action': 'replace', 'coverageRequired': True, 'coverage': coverage}
            if plan.policy == 'daily':
                record.update(action='keep', coverageRequired=True, coverage=coverage)
            if plan.policy != 'daily':
                decision = _chat_regression_reason(incoming, old)
                if decision:
                    reason, force = decision
                    guard = {'path': record['path'], 'reason': reason, 'forceEligible': force}
                    if guard not in action.guards:
                        action.guards.append(guard)
                    if not force or plan.policy == 'rename':
                        refuse('overwrite_guard')
                        return
    else:
        decision = _collision_failure_reason(action.source, evidence(action.target),
            range_media_root=plan.archive if plan.range_initial is not None else None,
            range_media_objects=plan.media_objects)
        if decision:
            reason, force = decision
            directory_parts = [os.path.normcase(p) for p in parts[:-1]]
            force = force and MEDIA_STORE_NAME not in directory_parts and 'media' not in directory_parts
            guard = {'path': action.target.relative_to(plan.archive).as_posix(),
                     'reason': reason, 'forceEligible': force}
            if guard not in action.guards:
                action.guards.append(guard)
            if not force or plan.policy != 'bulk':
                refuse('overwrite_guard')
                return
        record = {'path': action.target.relative_to(plan.archive).as_posix(),
                  'sha256': target_sha, 'action': 'replace', 'coverageRequired': False,
                  'backupRequired': True, 'backupBasis': 'non_chat_replacement',
                  'coverage': {'covered': False, 'basis': 'byte_backup'}}
    if action.target not in records:
        action.existing.append(record)
    elif record is not records[action.target]:
        records[action.target].clear()
        records[action.target].update(record)


def _planned_archive_removals(plan: RawIngestPlan) -> set[Path]:
    removed = {p for a in plan.actions if a.status == "ready" for p in a.retire}
    if plan.policy == "rename":
        removed.update(a.source for a in plan.actions if a.status == "ready"
                       and not a.retain_source and a.source != a.target)
    return removed


def _old_directories(plan: RawIngestPlan) -> set[Path]:
    paths = _planned_archive_removals(plan)
    if plan.policy == "rename":
        paths.update(a.source for a in plan.actions if a.kind == "chat")
    candidates = {plan.archive / p.relative_to(plan.archive).parts[0]
                  for p in paths if p.is_relative_to(plan.archive)
                  and len(p.relative_to(plan.archive).parts) > 1}
    active = {a.target.parent for a in plan.actions if a.kind == "chat" and a.status == "ready"}
    return candidates - active


def _directory_retention(plan: RawIngestPlan, directories: set[Path],
                         removed: set[Path] | None = None, *, after_cleanup: bool = False) -> list[dict]:
    """List remaining old-directory contents without opening or changing their bytes."""
    records = []
    for directory in sorted(directories):
        if not directory.exists():
            continue
        errors = []
        remaining = []
        for parent, _, files in os.walk(directory, onerror=errors.append):
            for name in files:
                path = Path(parent) / name
                if path not in (removed or set()):
                    remaining.append(path.relative_to(plan.archive).as_posix())
        reasons = []
        if any("media" in Path(p).parts[1:-1] for p in remaining):
            reasons.append("legacy_media_retained")
        if any("media" not in Path(p).parts[1:-1] for p in remaining):
            reasons.append("other_files_retained")
        if errors:
            reasons.append("directory_read_failed")
        if not reasons and after_cleanup:
            reasons.append("directory_cleanup_failed")
        if reasons:
            records.append({"directory": directory.relative_to(plan.archive).as_posix(),
                            "reasons": reasons, "remainingFiles": sorted(remaining)})
    return records


def _mark_rename_pending(plan: RawIngestPlan) -> None:
    if plan.policy != "rename":
        return
    warning = "同日多文件，待下次重写时收敛"
    removed = _planned_archive_removals(plan)
    final_files = defaultdict(set)
    for (wxid, day), paths in plan.index.by_key.items():
        for path in paths:
            if path not in removed:
                final_files[path.parent, wxid, day].add(path)
    for action in plan.actions:
        if warning in action.warnings:
            action.warnings.remove(warning)
        if action.kind == "chat" and action.status == "ready":
            final_files[action.target.parent, action.wxid, action.day].add(action.target)
    for action in plan.actions:
        if action.kind == "chat" and action.resolution == "rename" and len(
                final_files[action.target.parent, action.wxid, action.day]) > 1:
            action.warnings.append(warning)


def finalize_dependencies(plan: RawIngestPlan) -> None:
    """Close attachment/publication dependencies after *all* collision decisions."""
    groups = defaultdict(list)
    by_source = {a.source: a for a in plan.actions}
    for action in plan.actions:
        action.retain_source = action.range_review
        if action.group:
            groups[action.group].append(action)
    while True:
        changed = False
        bad_cas_roots = {a.source_root for a in plan.actions if a.status != "ready"
                         and Path(a.relative).parts[0] == MEDIA_STORE_NAME}
        bad_media_groups = {a.group for a in plan.actions if a.status != "ready" and a.kind == "file"
                            and (plan.policy != 'daily' or 'media' in Path(a.relative).parts[:-1])}
        for action in plan.actions:
            if action.status != "ready":
                continue
            predecessor = by_source.get(action.depends_on)
            if any(p not in by_source or by_source[p].status != 'ready' for p in action.ordered_after):
                action.block('range_predecessor_blocked', 'hardBlocked')
                changed = True
            elif predecessor is not None and predecessor.status != "ready":
                action.block("rename_predecessor_blocked")
                changed = True
            elif action.kind in {"chat", "range"} and (
                    (action.source_root in bad_cas_roots if plan.policy != 'daily' else
                     any(by_source.get(p) and by_source[p].status != 'ready' for p in action.cas_references))
                    or any(by_source.get(p) and by_source[p].status != 'ready' for p in action.cas_references)
                    or action.group in bad_media_groups):
                action.block("attachment_blocked", "hardBlocked")
                changed = True
        if not changed:
            break
    required_cas = {path for a in plan.actions if a.status != "ready" or a.range_review
                    for path in a.cas_references}
    for actions in groups.values():
        if any(a.kind == "chat" and a.status != "ready" for a in actions):
            for action in actions:
                if action.kind != "chat":
                    action.retain_source = True
    for action in plan.actions:
        if action.source in required_cas and Path(action.relative).parts[0] == MEDIA_STORE_NAME:
            action.retain_source = True
    _mark_rename_pending(plan)


@dataclass
class ApplyResult:
    published: list[Path] = field(default_factory=list)
    consumed: list[Path] = field(default_factory=list)
    retained: list[Path] = field(default_factory=list)
    retired: list[Path] = field(default_factory=list)
    duplicates: int = 0
    warnings: list[dict] = field(default_factory=list)
    backup_dir: Path | None = None
    range_review: bool = False


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if _fingerprint(temporary) != _sha(data):
            raise OSError("staged bytes failed verification")
        if path.exists():
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        path.unlink()


def remove_empty_directories(root: Path, *, remove_root: bool = False) -> None:
    if not root.is_dir():
        return
    directories = sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True)
    for directory in [*directories, *([root] if remove_root else [])]:
        try:
            directory.rmdir()
        except OSError:
            pass


def _source_signature(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino, info.st_ctime_ns


def validate_plan(plan: RawIngestPlan) -> dict[Path, tuple]:
    if plan.hypothetical:
        raise ValueError("analysis-only overrides cannot be executed")
    if _inventory(plan.archive) != plan.index.inventory:
        raise ValueError("archive membership changed after planning")
    if _context_signature(plan) != plan.checked_context:
        raise ValueError('plan order or range evidence changed after final inspection')
    if plan.range_initial is not None and _range_inventory(plan.archive) != plan.range_initial:
        raise ValueError('range archive changed after planning')
    if any(_fingerprint(p) != sha for p, sha in plan.range_evidence.items()):
        raise ValueError('range media changed after planning')
    validated_sources = {}
    for action in plan.actions:
        if action.status != "ready":
            continue
        if plan.checked_actions.get(action.source) != _execution_signature(action):
            raise ValueError('action changed after final target inspection')
        signature = _source_signature(action.source)
        if _fingerprint(action.source) != action.source_sha or _source_signature(action.source) != signature:
            raise ValueError("incoming changed after planning")
        validated_sources[action.source] = signature
        if _fingerprint(action.target) != action.target_before:
            raise ValueError("target changed after planning")
        for old in action.existing:
            if plan.archive / old['path'] in action.projected_objects:
                continue
            if _fingerprint(plan.archive / old["path"]) != old["sha256"]:
                raise ValueError("archive sibling changed after planning")
    return validated_sources


def _move_daily_attachment(plan: RawIngestPlan, action: RawAction, *, move_source: bool,
                           expected_target: str | None, signature: tuple) -> bool:
    parts = Path(action.relative).parts
    if (plan.policy != "daily" or action.kind != "file" or not move_source or action.retain_source
            or expected_target is not None or not (parts[0] == MEDIA_STORE_NAME or "media" in parts[:-1])):
        return False
    if _source_signature(action.source) != signature:
        raise ValueError("incoming changed before move")
    action.target.parent.mkdir(parents=True, exist_ok=True)
    if action.target.exists():
        raise ValueError("target changed before move")
    try:
        os.replace(action.source, action.target)
    except OSError:
        if action.target.exists():
            raise ValueError("target changed during move")
        # Cross-volume or other rename failures retain the existing verified-copy path.
        return False
    if action.source.exists() or _source_signature(action.target)[:4] != signature[:4]:
        raise OSError("attachment move was not observed")
    return True


def apply_raw_ingest_plan(plan: RawIngestPlan, *, move_source: bool = False,
                          force_overwrite: bool = False, include_ranges: bool = True) -> ApplyResult:
    """Execute an inspected plan; never resolve paths again during writing."""
    if _context_signature(plan) != plan.checked_context:
        raise ValueError('plan order or context changed after final inspection')
    finalize_dependencies(plan)
    if plan.policy == "bulk" and any(a.status != "ready" for a in plan.actions):
        raise ValueError("raw plan has hard blockers; no files written")
    if plan.policy == "bulk" and not force_overwrite and any(a.guards for a in plan.actions):
        raise ValueError("raw plan requires explicit overwrite confirmation")
    validated_sources = validate_plan(plan)
    result = ApplyResult(range_review=any(a.range_review for a in plan.actions))
    manifest = {"schemaVersion": 1, "entrypoint": plan.policy, "items": []}
    current_targets = {a.target: a.target_before for a in plan.actions}
    saved_versions = {}

    def backup_destination(old: dict) -> Path:
        relative = Path(old['path'])
        if not relative.parts or relative.anchor or '..' in relative.parts:
            raise ValueError('unsafe archive-relative backup path')
        directory = result.backup_dir
        candidate = directory / relative
        number = 0
        while True:
            ledger = directory / 'manifest.json'
            blocked = candidate == ledger or ledger in candidate.parents
            parent = candidate.parent
            while parent != directory:
                if parent.exists() and not parent.is_dir():
                    blocked = True
                    break
                parent = parent.parent
            if not blocked and _fingerprint(candidate) in (None, old['sha256']):
                return candidate
            # Raw-relative names can occupy the ledger or a generated version
            # slot, even its directory prefix. Never overwrite another backup.
            prefix = '_versions' if number == 0 else f'_versions-{number}'
            candidate = directory / prefix / old['sha256'] / relative
            number += 1

    def backup(action: RawAction, *, skip_target: bool = False) -> None:
        to_backup = [old for old in action.existing if old["action"] != "keep"
                     and not ((action.duplicate or action.noop or skip_target) and plan.archive / old['path'] == action.target)
                     and not (action.retain_source and plan.archive / old['path'] == action.source)]
        if not to_backup:
            return
        if result.backup_dir is None:
            parent = plan.archive.parent / "raw-folded"
            parent.mkdir(parents=True, exist_ok=True)
            result.backup_dir = parent / (datetime.now().strftime("%Y%m%d-%H%M%S") +
                                          f"-{plan.policy}-{uuid.uuid4().hex[:8]}")
            result.backup_dir.mkdir()
        for old in to_backup:
            original = plan.archive / old["path"]
            data = original.read_bytes()
            if _sha(data) != old["sha256"]:
                raise ValueError("archive changed before backup")
            saved = backup_destination(old)
            _atomic_bytes(saved, data)
            if _fingerprint(saved) != old["sha256"] or _fingerprint(original) != old["sha256"]:
                raise OSError("backup reread failed")
            saved_versions[original, old['sha256']] = saved
            manifest["items"].append(dict(old, backupPath=saved.relative_to(result.backup_dir).as_posix(),
                                           target=action.target.relative_to(plan.archive).as_posix(),
                                           incomingSha256=action.source_sha, status="prepared"))
        _atomic_bytes(result.backup_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        for old in to_backup:
            saved = saved_versions[plan.archive / old['path'], old['sha256']]
            if _fingerprint(saved) != old['sha256']:
                raise OSError('backup bytes changed after ledger publication')

    # Attachments must be present before a JSON discovery marker is published.
    actions = sorted(plan.actions, key=lambda a: 2 if a.kind == 'range' else int(a.kind == 'chat'))
    for action in actions:
        if action.status != "ready" or action.kind == "range" and not include_ranges:
            result.retained.append(action.source)
            if action.status != "ready":
                result.warnings.append({"source": str(action.source), "reasons": action.reasons})
            continue
        expected_target = action.projected_objects.get(action.target, (None, current_targets[action.target]))[1]
        if _fingerprint(action.target) != expected_target:
            raise ValueError("target changed before publication")
        # An earlier action in this exact plan may already have published the
        # same bytes. It is a no-op, not an external change or a second backup.
        noop = action.noop or expected_target == action.source_sha
        for old in action.existing:
            if noop and plan.archive / old['path'] == action.target:
                continue
            if _fingerprint(plan.archive / old["path"]) != old["sha256"]:
                raise ValueError("archive sibling changed before publication")
        moved = _move_daily_attachment(plan, action, move_source=move_source,
                    expected_target=expected_target, signature=validated_sources[action.source])
        if not moved:
            data = action.source.read_bytes()
            if _sha(data) != action.source_sha:
                raise ValueError("incoming changed before publication")
            # Final inspection records exactly which old bytes need a backup.
            folding = (bool(action.retire) or plan.policy == "rename" and bool(action.existing)
                       or any(old.get("coverageRequired") or old.get('backupRequired') for old in action.existing))
            if folding:
                backup(action, skip_target=noop)
            if not action.duplicate and not noop:
                _atomic_bytes(action.target, data)
            if _fingerprint(action.target) != action.source_sha:
                raise OSError("published bytes failed verification")
        current_targets[action.target] = action.source_sha
        result.published.append(action.target)
        for old_path in action.retire:
            if old_path == action.source and action.retain_source:
                continue
            expected = next(e["sha256"] for e in action.existing if plan.archive / e["path"] == old_path)
            if _fingerprint(old_path) != expected:
                raise ValueError("archive sibling changed before retirement")
            saved = saved_versions.get((old_path, expected))
            if saved is None or _fingerprint(saved) != expected:
                raise OSError("retirement backup is not readable")
            try:
                _unlink(old_path)
            except OSError as exc:
                record = next(e for e in action.existing if plan.archive / e['path'] == old_path)
                if record.get('retirementBasis') != 'moments_coverage':
                    raise
                result.range_review = True
                action.retain_source = True
                result.warnings.append({'source': str(action.source), 'reasons': ['range_retirement_failed']})
                print(f'[WARN] 保留旧区间文件 {old_path.name}：删除失败：{exc}')
                continue
            result.retired.append(old_path)
            current_targets[old_path] = None
        if action.duplicate:
            result.duplicates += 1
        if action.retain_source:
            result.retained.append(action.source)
        else:
            result.consumed.append(action.source)
        if move_source and not action.retain_source and action.source.exists() and action.source != action.target:
            if _fingerprint(action.source) != action.source_sha:
                raise ValueError("incoming changed before source removal")
            _unlink(action.source)
        if action.warnings:
            result.warnings.append({"source": str(action.source), "target": str(action.target),
                                    "reasons": action.warnings})
        if result.backup_dir is not None:
            for entry in manifest["items"]:
                if entry["target"] == action.target.relative_to(plan.archive).as_posix():
                    entry["status"] = "published"
            _atomic_bytes(result.backup_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    old_directories = _old_directories(plan)
    for directory in old_directories:
        remove_empty_directories(directory, remove_root=True)
    result.warnings.extend(_directory_retention(plan, old_directories, after_cleanup=True))
    if move_source:
        for root in plan.roots:
            if root != plan.archive:
                remove_empty_directories(root)
    return result


def _report_item_key(item: dict) -> tuple[str, ...]:
    owner = str(item.get("entrypoint") or "daily")
    identity = item.get("identity") or {}
    if identity.get("wxid") and identity.get("day"):
        return owner, "chat", str(identity["wxid"]), str(identity["day"])
    if "directory" in item:
        return owner, "directory", str(item["directory"])
    if "source" in item:
        return owner, "source", str(item["source"])
    return owner, "legacy", json.dumps(item, ensure_ascii=False, sort_keys=True)


def _observed_day_files(plan: RawIngestPlan, actions: list[RawAction], previous: dict) -> tuple[list[str], bool]:
    wxid, day = actions[0].wxid, actions[0].day
    if not wxid or not day:
        return [], True
    candidates = set(plan.index.by_key.get((wxid, day), []))
    candidates.update(a.target for a in actions)
    directories = {plan.archive / name for name in plan.index.directories.get(wxid, set())}
    directories.update(a.target.parent for a in actions)
    suffix = "_" + day.replace("-", "") + ".json"
    for directory in directories:
        candidates.update(p for p in directory.glob("*.json") if p.name.lower().endswith(suffix))
    # Earlier fallback paths can be root/nested files outside the supported index.
    for relative in previous.get("archiveFiles", []):
        path = (plan.archive / relative).resolve()
        if path.is_relative_to(plan.archive):
            candidates.add(path)
    found, unknown = [], False
    for path in candidates:
        if not path.is_file() or _day(path) != day:
            continue
        session = _read_stored_session(path)
        observed = str((session or {}).get("wxid") or "").strip()
        if not observed:
            unknown = True
            # Keep the unresolved candidate across runs until it can be read
            # or is gone; otherwise a second observation could falsely clear it.
            found.append(path.relative_to(plan.archive).as_posix())
        elif observed == wxid:
            found.append(path.relative_to(plan.archive).as_posix())
    return sorted(found), unknown


def _updated_report_items(plan: RawIngestPlan, result: ApplyResult, previous: dict | None) -> list[dict]:
    previous = previous or {}
    consumed_sources = set(result.consumed)
    published_targets = set(result.published)
    by_source = {str(a.source): a for a in plan.actions if a.kind == "chat" or a.report_context.get('finalTarget')}
    ledger = {}
    for old in previous.get("items", []):
        item = dict(old)
        item.setdefault("entrypoint", previous.get("entrypoint", "daily"))
        # Preserve unkeyed v1 evidence; migrate it only when its source is observed again.
        action = by_source.get(str(item.get("source") or ""))
        identity = item.get('identity') or {}
        if item["entrypoint"] == plan.policy and action and not (identity.get('wxid') and identity.get('day')):
            item["identity"] = {"wxid": action.wxid, "day": action.day}
        ledger[_report_item_key(item)] = item
    groups = defaultdict(list)
    for action in plan.actions:
        if action.kind == "chat" or action.report_context.get('finalTarget'):
            identity = {"wxid": action.wxid, "day": action.day}
            key = _report_item_key({"entrypoint": plan.policy, "identity": identity, "source": str(action.source)})
            groups[key].append(action)
        else:
            key = (plan.policy, "source", str(action.source))
            if action.status != "ready" or action.range_review:
                ledger[key] = {"entrypoint": plan.policy, "source": str(action.source),
                               "reasons": [*action.reasons, *action.warnings]}
            elif action.source in consumed_sources:
                ledger.pop(key, None)
    for key, actions in groups.items():
        files, unknown = _observed_day_files(plan, actions, ledger.get(key, {}))
        fallback = any(a.resolution == "legacy_fallback" for a in actions)
        published = all(a.status == "ready" and a.target in published_targets for a in actions)
        if len(files) == 1 and not unknown and not fallback and published:
            ledger.pop(key, None)
            continue
        reasons = {r for a in actions for r in [*a.reasons, *a.warnings]}
        if len(files) != 1:
            reasons.add(f"archive_day_file_count:{len(files)}")
        if unknown:
            reasons.add("archive_identity_unresolved")
        item = {"entrypoint": plan.policy, "identity": {"wxid": actions[0].wxid, "day": actions[0].day},
                "sources": sorted({str(a.source) for a in actions}),
                "targets": sorted({a.target.relative_to(plan.archive).as_posix() for a in actions}),
                "archiveFiles": files, "reasons": sorted(reasons)}
        details = []
        for action in actions:
            context = action.report_context
            incoming = context.get('incoming') or _chat_description(action.source)
            legacy = _legacy_target(plan.archive, Path(action.relative))
            target = context.get('finalTarget') or context.get('legacyTarget') or _chat_description(legacy)
            details.append({'incoming': incoming, 'legacyTarget': target,
                            'retainedSource': str(action.source) if action.source not in consumed_sources else None})
        item['details'] = details
        item['affectedIdentities'] = [d['legacyTarget']['identity'] for d in details
                                      if d['legacyTarget']['identity']['wxid']]
        if key[1] == "source":
            item["source"] = str(actions[0].source)
        ledger[key] = item
    retained_directories = {item["directory"]: item for item in result.warnings if "directory" in item}
    for path in _old_directories(plan):
        directory = path.relative_to(plan.archive).as_posix()
        key = (plan.policy, "directory", directory)
        if directory in retained_directories:
            ledger[key] = dict(retained_directories[directory], entrypoint=plan.policy)
        elif not path.exists():
            ledger.pop(key, None)
    return [ledger[key] for key in sorted(ledger)]


REPORT_OPENER = (
    '请读取本报告所在目录的 raw-fold-report.json，并以同目录的 raw/ 为归档根，'
    '逐项核对报告中 incoming、最终目标和旧副本的身份、消息覆盖及媒体引用。'
    '先只读取证，再按项目授权和备份规则处理能证明安全的项目并复查；'
    '无法裁定的项目保留原件，列出具体差异和待裁决问题。不要直接覆盖、删除或清空保留输入。'
)


def _report_markdown(plan: RawIngestPlan, items: list[dict]) -> str:
    lines = ['# 归档待处理报告', '', '复制下面这段开场词，并把本报告交给 Agent：', '',
             '```text', REPORT_OPENER, '```', '', f'归档根：{plan.archive}',
             '机器明细：raw-fold-report.json。条目保留到再次处理并确认解决；历史条目可能需要重新取证。',
             '处理完对应 raw 再删账本；提前删除会使非轮转入口失去保留记录，可能影响可从归档 raw 重建的 processed Markdown。下次轮转会重新核对并记账。', '']
    if not items:
        return '\n'.join([*lines, 'raw 已无待处理项；processed 影响见下文（如有）。', ''])
    for number, item in enumerate(items, 1):
        reasons = item.get('reasons', [])
        if 'fallback_target_other_wxid' in reasons or 'final_target_other_identity' in reasons:
            happened = '最终目标属于另一个会话或日期，已阻止替换并保留 incoming。'
        elif 'fallback_target_not_covered' in reasons or 'final_target_not_covered' in reasons:
            happened = 'incoming 未覆盖最终目标的全部消息，已阻止替换并保留 incoming。'
        elif 'invalid_incoming_chat' in reasons:
            happened = 'incoming 不是可校验的聊天 JSON，已阻止它替换现存归档件。'
        elif 'fallback_target_identity_unknown' in reasons:
            happened = '按名目标的会话身份无法确认，已保留原件和 incoming，等待核对。'
        elif any(str(reason).startswith('archive_unreadable:') for reason in reasons):
            happened = '归档旧件正文无法校验，已按退路处理；同身份坏目标在备份复读后才被替换。'
        elif any(str(reason).startswith('coverage_failed_fallback:') for reason in reasons):
            happened = 'incoming 未覆盖全部归档兄弟，已保留未覆盖旧件并采用受保护的按名退路。'
        else:
            happened = '本次未能确认该项已安全收敛，需要核对保留文件与归档状态。'
        lines.extend([f'## 待处理 {number}', '', happened, '',
                      f"入口：{item.get('entrypoint', 'daily')}；身份键：{json.dumps(item.get('identity', {}), ensure_ascii=False)}", ''])
        details = item.get('details', [])
        for detail in details:
            for label, record in [('最终目标', detail['legacyTarget']), ('incoming', detail['incoming'])]:
                count = record.get('messageCount')
                count_text = f'{count} 条' if count is not None else '未知（不可读取或旧报告未记录，不代表 0 条）'
                identity = record.get('identity', {})
                lines.append(f"- {label}：{record.get('displayName') or '名称未知'}；"
                             f"wxid={identity.get('wxid') or '未知'}；日期={identity.get('day') or '未知'}；"
                             f"消息 {count_text}；路径：{record.get('path') or '未记录'}")
            retained = detail.get('retainedSource')
            lines.append(f'- live raw 保留位置：{retained or "本次已发布，源已消费；请核对归档文件。"}')
            if retained:
                from .processed_reporting import PROCESSED_GUIDANCE
                lines.append('- ' + PROCESSED_GUIDANCE)
            lines.append('- 不处理的影响：' + ('每天轮转会再次遇到这份输入；问题仍在时继续保留它及已识别的媒体引用，报告内容相同不会再弹窗。'
                                                   if retained else '每天报告仍保留此项；未再次处理并确认解决前，归档旧副本或歧义不会自动消失。'))
        if not details:
            lines.extend(['- 目标与 incoming 身份、消息数：旧报告或目录项未记录，须读取明细路径核实。',
                          '- live raw 保留位置：' + str(item.get('source') or item.get('sources') or '未记录；见机器明细'),
                          '- 不处理的影响：每天清单继续保留，相关目录或历史问题不会自动消失。'])
        lines.extend(['', '机器原因：' + '；'.join(str(r) for r in reasons),
                      '归档证据路径：' + json.dumps(item.get('archiveFiles', item.get('directory', [])), ensure_ascii=False), ''])
    return '\n'.join(lines)


def validate_archive_report(document: dict) -> None:
    """Validate consumed fields, preserving optional fields of legacy reports."""
    def require(condition):
        if not condition:
            raise ValueError('invalid archive report structure')

    def text_fields(value, fields):
        require(isinstance(value, dict))
        for field in fields:
            require(field not in value or value[field] is None or isinstance(value[field], str))

    def identity(value):
        text_fields(value, ('wxid', 'day'))

    require(isinstance(document, dict) and isinstance(document.get('items'), list))
    require(document.get('schemaVersion', 1) in (1, 2))
    text_fields(document, ('entrypoint',))
    for item in document['items']:
        text_fields(item, ('entrypoint', 'source', 'directory'))
        if 'identity' in item:
            identity(item['identity'])
        for field in ('reasons', 'sources', 'targets', 'archiveFiles'):
            if field in item:
                require(isinstance(item[field], list) and all(isinstance(v, str) for v in item[field]))
        if 'affectedIdentities' in item:
            require(isinstance(item['affectedIdentities'], list))
            for value in item['affectedIdentities']:
                identity(value)
        if 'details' not in item:  # v1 / directory evidence may have no details
            continue
        require(isinstance(item['details'], list))
        for detail in item['details']:
            text_fields(detail, ('retainedSource',))
            for field in ('incoming', 'legacyTarget'):
                require(field in detail)
                record = detail[field]
                text_fields(record, ('path', 'displayName'))
                if 'identity' in record:
                    identity(record['identity'])
                count = record.get('messageCount')
                require(count is None or type(count) is int and count >= 0)
                require('exists' not in record or isinstance(record['exists'], bool))


def report_daily_result(plan: RawIngestPlan, result: ApplyResult, *, report_opener=None) -> None:
    """Accumulate per-entrypoint session/day evidence; clear only observed resolved keys."""
    path = plan.archive.parent / "raw-fold-report.json"
    previous = None
    recovered = False
    try:
        try:
            previous = json.loads(path.read_text(encoding="utf-8-sig"))
            validate_archive_report(previous)
        except FileNotFoundError:
            pass
        except (ValueError, UnicodeError):
            corrupt = path.with_name('raw-fold-report.corrupt-' +
                datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-' + uuid.uuid4().hex[:8] + '.json')
            os.replace(path, corrupt)
            print(f'[WARN] 归档报告损坏，原字节已封存：{corrupt}')
            previous, recovered = None, True
        payload = {"schemaVersion": 2, "items": _updated_report_items(plan, result, previous)}
        if not payload['items'] and previous is None and not recovered:
            return
        if payload != previous:
            _atomic_bytes(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        markdown_path = path.with_suffix('.md')
        if not payload['items'] and not markdown_path.exists():
            return
        markdown = _report_markdown(plan, payload['items']).encode('utf-8')
        if markdown_path.exists() and markdown_path.read_bytes() == markdown:
            return
        _atomic_bytes(markdown_path, markdown)
        if payload['items']:
            print(f"[WARN] 归档待审查 {len(payload['items'])} 项，详情与处理开场词：{markdown_path}")
            if report_opener is not None:
                try:
                    report_opener(markdown_path)
                except Exception:  # advisory UI must never gate the export
                    print(f'[WARN] 无法打开归档报告，请手动打开：{markdown_path}')
    except Exception as exc:  # advisory persistence/rendering must not abort rotation
        print(f"[WARN] 归档待审查报告未能更新，原报告保留：{type(exc).__name__}")
