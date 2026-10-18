"""Persistent success/label caches and payload-free attempt diagnostics."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading

from .vision_prompts import PROMPT_VERSION, LABEL_VERSION, OUTPUT_SCHEMA, LABEL_SCHEMA
from .vision_records import VisionError, validate
from .vision_units import ImageItem, text_sha


_locks_guard = threading.Lock()
_locks: dict[str, list] = {}
_signal_indices: dict[str, dict] = {}


def _phash_insert(tree, value, entry):
    if tree is None:
        return [value, [entry], {}]
    node = tree
    while True:
        distance = (value ^ node[0]).bit_count()
        if distance == 0:
            node[1].append(entry)
            return tree
        if distance not in node[2]:
            node[2][distance] = [value, [entry], {}]
            return tree
        node = node[2][distance]


def _phash_near(tree, value, threshold=6):
    result, pending = [], [tree] if tree is not None else []
    while pending:
        node = pending.pop()
        distance = (value ^ node[0]).bit_count()
        if distance <= threshold:
            result.extend(node[1])
        pending.extend(child for edge, child in node[2].items() if distance - threshold <= edge <= distance + threshold)
    return result


@contextmanager
def single_flight(key: str):
    with _locks_guard:
        slot = _locks.setdefault(key, [threading.RLock(), 0])
        slot[1] += 1
    try:
        with slot[0]:
            yield
    finally:
        with _locks_guard:
            slot[1] -= 1
            if slot[1] == 0:
                _locks.pop(key, None)


def read_json(path: Path):
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        return body if isinstance(body, dict) else None
    except (OSError, ValueError):
        return None


def atomic_json(path: Path, body: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".vision-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(body, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class VisionStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def path(self, image_sha: str, context_sha: str, model: str, version: str = PROMPT_VERSION) -> Path:
        key = text_sha("\0".join((image_sha, context_sha, version, model)))
        return self.root / image_sha[:2] / image_sha / f"{key}.json"

    def lookup(self, image_sha: str, context_sha: str, model: str, version: str = PROMPT_VERSION):
        exact = self.path(image_sha, context_sha, model, version)
        candidates = [exact] + sorted(exact.parent.glob("*.json"))
        seen = set()
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            entry = read_json(path)
            if not entry or any(entry.get(k) != v for k, v in (
                ("image_sha256", image_sha), ("prompt_version", version), ("model", model))):
                continue
            if not isinstance(entry.get("context_sha256"), str) or len(entry["context_sha256"]) != 64:
                continue
            try:
                validate(entry.get("record"), OUTPUT_SCHEMA["properties"]["images"]["items"])
                if not entry["record"]["gist"].strip():
                    continue
            except VisionError:
                continue
            return entry, entry.get("context_sha256") != context_sha
        return None, False

    def put(self, entry: dict):
        atomic_json(self.path(entry["image_sha256"], entry["context_sha256"], entry["model"],
                              entry["prompt_version"]), entry)

    def label_path(self, image_sha: str, model: str, version: str = LABEL_VERSION) -> Path:
        key = text_sha("\0".join((image_sha, version, model)))
        return self.root / "_labels" / image_sha[:2] / f"{key}.json"

    def label(self, image_sha: str, model: str):
        entry = read_json(self.label_path(image_sha, model))
        if not entry or (entry.get("image_sha256"), entry.get("label_version"), entry.get("model")) != (image_sha, LABEL_VERSION, model):
            return None
        try:
            validate(entry.get("value"), LABEL_SCHEMA["properties"]["items"]["items"])
            if len(entry["value"]["anchor"]) > 12:
                return None
        except VisionError:
            return None
        return entry["value"]

    def put_label(self, image_sha: str, model: str, value: dict):
        atomic_json(self.label_path(image_sha, model), dict(image_sha256=image_sha, label_version=LABEL_VERSION,
                                                        model=model, value=value))

    def diagnostic(self, kind: str, **values):
        # No prompt, payload, labels, output, path, nickname, or stderr is accepted.
        allowed = {k: v for k, v in values.items() if k in {
            "pack_id", "pack_size", "group_sizes", "slots", "reason", "attempt", "checks", "detail_chars", "render_detail_chars"}}
        row = dict(kind=kind, created_at=datetime.now(timezone.utc).isoformat(), **allowed)
        path = self.root / "_runs" / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with single_flight(str(path)):
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def signal(self, item: ImageItem) -> dict:
        """pHash clusters annotate only; neither merge nor skip any image."""
        key = str(self.root)
        with single_flight(key + ":signals"):
            if key not in _signal_indices:
                data, ranks, tree = {}, {}, None
                for p in (self.root / "_signals").glob("*/*.json"):
                    value = read_json(p)
                    if not value or not all(k in value for k in ("image_sha256", "phash_cluster", "cluster_rank")):
                        continue
                    data[p.stem] = value
                    cluster = value["phash_cluster"]
                    ranks[cluster] = max(ranks.get(cluster, 0), value["cluster_rank"])
                    if value.get("phash"):
                        try:
                            tree = _phash_insert(tree, int(value["phash"], 16), value)
                        except (ValueError, TypeError):
                            pass
                _signal_indices[key] = dict(data=data, ranks=ranks, tree=tree)
            state = _signal_indices[key]
            index = state["data"]
            value = index.get(item.sha256)
            if value is None:
                matches = _phash_near(state["tree"], int(item.phash, 16)) if item.phash else []
                representative = min(matches, key=lambda v: (v.get("cluster_rank", 0), v["image_sha256"])) if matches else None
                cluster = representative["phash_cluster"] if representative else item.sha256
                rank = 1 + state["ranks"].get(cluster, 0)
                value = dict(image_sha256=item.sha256, md5=item.md5, image_size=list(item.size),
                             small_image=item.small, phash=item.phash, phash_cluster=cluster, cluster_rank=rank)
                index[item.sha256] = value
                state["ranks"][cluster] = rank
                if item.phash:
                    state["tree"] = _phash_insert(state["tree"], int(item.phash, 16), value)
                atomic_json(self.root / "_signals" / item.sha256[:2] / f"{item.sha256}.json", value)
            return value
