"""Publish and retire range Moments snapshots with one conservative predicate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .raw_schema import validate_moments_json


RANGE_PATTERN = re.compile(
    r"^朋友圈导出_区间_(?:[0-9]{8}-[0-9]{8}|空)_([0-9a-f]{8})"
    r"(?:_保留_[0-9a-f]{32})?\.json$"
)
REVIEW_EXIT_CODE = 3


def range_digest(path: Path) -> str | None:
    match = RANGE_PATTERN.fullmatch(path.name)
    return match.group(1) if match else None


@dataclass(frozen=True)
class Coverage:
    reasons: tuple[str, ...]
    posts: int = 0
    changes: tuple[tuple[str, int], ...] = ()

    @property
    def covered(self) -> bool:
        return not self.reasons


@dataclass(frozen=True)
class RangePublication:
    path: Path
    needs_review: bool


def _read(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    validate_moments_json(data)
    return data


def _posts(data: dict) -> dict[tuple[str, str], dict]:
    result = {}
    accounts = data["filters"]["usernames"]
    if not isinstance(accounts, list) or any(not isinstance(v, str) or not v for v in accounts):
        raise ValueError("filters.usernames 无有效账号集合")
    for post in data["posts"]:
        key = (post["username"], post["id"])
        if not all(isinstance(v, str) and v for v in key) or key[0] not in accounts or key in result:
            raise ValueError(f"动态身份为空、重复或不在账号集合：{key}")
        result[key] = post
    return result


def _references(post: dict) -> list[tuple[str, str]]:
    refs = []
    for media in post["media"]:
        if media.get("localPath"):
            refs.append(("media", media["localPath"]))
        live = media.get("livePhoto")
        if isinstance(live, dict) and live.get("localPath"):
            refs.append(("livePhoto", live["localPath"]))
    return refs


def _media_path(root: Path, reference: str) -> Path:
    path = (root / reference).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"媒体引用越界：{reference}")
    return path


def _fingerprint(path: Path) -> tuple[int, str]:
    digest = hashlib.md5()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    actual = digest.hexdigest()
    if re.fullmatch(r"[0-9a-fA-F]{32}", path.stem) and path.stem.lower() != actual:
        raise ValueError(f"CAS 内容 md5 不符：{path.name}")
    return size, actual


def _same_bytes(first: Path, second: Path) -> bool:
    with first.open("rb") as a, second.open("rb") as b:
        while True:
            x, y = a.read(1024 * 1024), b.read(1024 * 1024)
            if x != y:
                return False
            if not x:
                return True


def compare_coverage(old: Path, new: Path, *, new_media_root: Path | None = None) -> Coverage:
    """Only post identities and landed media constrain retirement; metadata may change.

    Staged JSON references are already anchored to its eventual publication root.
    Digest and the actual account set must both match; dates never prove coverage.
    """
    if range_digest(old) is None or range_digest(old) != range_digest(new):
        return Coverage(("账号 digest 不同或不是区间文件",))
    try:
        previous, incoming = _read(old), _read(new)
        old_posts, new_posts = _posts(previous), _posts(incoming)
        if set(previous["filters"]["usernames"]) != set(incoming["filters"]["usernames"]):
            return Coverage(("实际账号集合不同",))
        reasons = []
        changes = {key: 0 for key in ("comments", "likes", "nickname", "exportTime")}
        changes["exportTime"] = int(previous.get("exportTime") != incoming.get("exportTime"))
        for key, post in old_posts.items():
            current = new_posts.get(key)
            if current is None:
                reasons.append(f"缺少 post {key[0]}/{key[1]}")
                continue
            for field in ("comments", "likes", "nickname"):
                changes[field] += int(post.get(field) != current.get(field))
            new_refs = _references(current)
            for role, reference in _references(post):
                try:
                    old_path = _media_path(old.parent, reference)
                    identity = _fingerprint(old_path)
                    matched = False
                    errors = []
                    for new_role, new_reference in new_refs:
                        if new_role != role:
                            continue
                        try:
                            new_path = _media_path(new_media_root or new.parent, new_reference)
                            if _fingerprint(new_path) == identity and _same_bytes(old_path, new_path):
                                matched = True
                                break
                        except (OSError, ValueError) as exc:
                            errors.append(f"{new_reference}: {exc}")
                    if not matched:
                        detail = "; ".join(errors)
                        reasons.append(f"post {key[1]} 缺少或不可读 {role} {reference}" + (f"（{detail}）" if detail else ""))
                except (OSError, ValueError) as exc:
                    reasons.append(f"post {key[1]} 旧媒体不可核验 {role} {reference}：{exc}")
        return Coverage(tuple(reasons), len(old_posts), tuple(changes.items()))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return Coverage((f"区间快照不可核验：{exc}",))


def publish_range(source: Path, destination: Path) -> RangePublication:
    """Publish JSON after media, then retire covered siblings. Never delete CAS.

    An unsafe same-name collision publishes alongside the untouched old JSON.
    Failure to retire an old file is advisory: the new publication stays valid.
    """
    digest = range_digest(destination)
    if digest is None:
        raise ValueError(f"不是朋友圈区间文件：{destination.name}")
    _read(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A sibling temporary path carries the destination name for digest checks.
    staging = destination.parent / f".moments-publish-{uuid.uuid4().hex}"
    staging.mkdir()
    staged = staging / destination.name
    try:
        shutil.copy2(source, staged)
        incoming = _read(staged)
        # Publication is not successful while any declared local attachment is
        # unreadable or corrupt, even if the old snapshot has no media at all.
        for post in incoming["posts"]:
            for _role, reference in _references(post):
                _fingerprint(_media_path(destination.parent, reference))
        target = destination
        same_name = None
        if destination.exists():
            same_name = compare_coverage(destination, staged, new_media_root=destination.parent)
            if not same_name.covered:
                stem = destination.stem.split("_保留_", 1)[0]
                target = destination.with_name(f"{stem}_保留_{uuid.uuid4().hex}.json")
        os.replace(staged, target)
        needs_review = False
        if same_name is not None and same_name.covered:
            _report_replaced(destination, same_name)
        for old in sorted(destination.parent.iterdir()):
            if old == target or range_digest(old) != digest:
                continue
            coverage = compare_coverage(old, target)
            if not coverage.covered:
                needs_review = True
                print(f"[WARN] 保留旧区间文件 {old.name}：" + "；".join(coverage.reasons))
                continue
            try:
                old.unlink()
            except OSError as exc:
                needs_review = True
                print(f"[WARN] 保留旧区间文件 {old.name}：删除失败：{exc}")
            else:
                _report_replaced(old, coverage)
        return RangePublication(target, needs_review)
    finally:
        shutil.rmtree(staging)


def _report_replaced(old: Path, coverage: Coverage) -> None:
    counts = "；".join(f"{field} 变化 {count}" for field, count in coverage.changes)
    print(f"已替换旧区间文件 {old.name}（{coverage.posts} 条全部被新文件覆盖）；{counts}")
