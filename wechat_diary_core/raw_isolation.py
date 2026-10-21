"""Move daily retained inputs to byte-verified, independently ingestible roots."""
from datetime import datetime
import hashlib
import json
from pathlib import Path


def retention_root(archived_root):
    archived = Path(archived_root).resolve()
    return archived.with_name(archived.name + "-retained")


def check_isolation_roots(plan):
    # This is a write precondition, never part of planning or its serialization.
    from .raw_archive import reject_overlapping_roots
    destination = retention_root(plan.archive.parent)
    reject_overlapping_roots(plan.archive.parent, [destination])
    for source in plan.roots:
        reject_overlapping_roots(source, [destination])
    return destination


def _warn(result, source, exc):
    reason = "isolation_failed:" + type(exc).__name__
    result.warnings.append({"source": str(source), "reasons": [reason]})
    message = f"[WARN] 隔离未完成，原文件保留在 live：{source}（{exc}）"
    print(message.encode("utf-8", errors="backslashreplace").decode("utf-8"))


def prepare_isolation(plan, result):
    """Capture live dependencies before apply consumes CAS, without changing the plan."""
    from . import raw_archive as raw
    for action in plan.actions:
        if action.source.suffix.lower() != ".json":
            continue
        try:
            data = action.source.read_bytes()
            if raw._sha(data) != action.source_sha:
                raise ValueError("incoming changed before isolation preparation")
            payload = raw._json_prefix_for_media(data)
            refs = raw._chat_cas_references(payload, action.source, plan.roots[action.source_root])
            result.isolation_dependencies[action.source] = {
                path: raw._fingerprint(path) for path in refs if path.is_file()}
        except (OSError, ValueError) as exc:
            _warn(result, action.source, exc)
    # A per-file collection failure still leaves the planner's known dependencies
    # available for keeping that source live. These are never changed in the plan.
    return ({path for refs in result.isolation_dependencies.values() for path in refs} |
            {path for action in plan.actions for path in action.cas_references})


def release_unused_media(plan, result):
    """Successful discovery markers no longer need their deferred live media."""
    from . import raw_archive as raw
    retained = set(result.retained)
    consumed = set(result.consumed)
    deferred = ({path for refs in result.isolation_dependencies.values() for path in refs} |
                {path for action in plan.actions for path in action.cas_references})
    for action in plan.actions:
        if action.source not in consumed or action.source in retained:
            continue
        if action.source not in deferred:
            continue
        try:
            if action.source.exists() and action.source != action.target:
                if raw._fingerprint(action.source) != action.source_sha:
                    raise ValueError("deferred media changed before source removal")
                raw._unlink(action.source)
        except (OSError, ValueError) as exc:
            result.retained.append(action.source)
            result.consumed.remove(action.source)
            _warn(result, action.source, exc)


def isolate_retained(plan, result):
    from . import raw_archive as raw
    if plan.policy != "daily" or not result.retained:
        return
    by_source = {a.source: a for a in plan.actions}
    held = set(result.retained)
    # Publishing an attachment is independent of whether a later range can be
    # retired. Keep its original live bytes until that outcome is known.
    required = {path for source in held for path in
                set(result.isolation_dependencies.get(source, {})) | by_source[source].cas_references}
    preparation_failed = {Path(warning['source']) for warning in result.warnings
                          if any(reason.startswith('isolation_failed:') for reason in warning.get('reasons', []))}
    for path in sorted(required - held):
        if path in by_source and path.is_file():
            result.retained.append(path)
            held.add(path)
    result.consumed[:] = [path for path in result.consumed if path not in held]

    def warn(source, exc):
        _warn(result, source, exc)

    try:
        parent = check_isolation_roots(plan)
        parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        number = 0
        while True:
            directory = parent / (stamp if number == 0 else f"{stamp}-{number}")
            manifest_path = parent / (directory.name + ".manifest.json")
            if not manifest_path.exists():
                try:
                    directory.mkdir()
                    break
                except FileExistsError:
                    pass
            number += 1
    except (OSError, ValueError) as exc:
        warn(plan.roots[0], exc)
        return
    result.isolation_dir = directory
    copied, records = {}, []
    recovered = set()
    when = datetime.now().astimezone().isoformat()
    warning_reasons = {}
    for warning in result.warnings:
        warning_reasons.setdefault(warning.get("source"), set()).update(warning.get("reasons", []))

    def record(source, target, sha, action=None):
        row = {"source": str(source), "path": target.relative_to(directory).as_posix(),
               "sha256": sha, "identity": {"wxid": action.wxid if action else "",
                                            "day": action.day if action else None},
               "reasons": sorted(set([*action.reasons, *action.warnings] if action else []) |
                                 warning_reasons.get(str(source), set())), "rotatedAt": when}
        # Validate each row before publishing its marker. A surrogate filename
        # must fail this file, not the whole batch after copies have appeared.
        json.dumps(row, ensure_ascii=False).encode("utf-8")
        return row

    def recover_dependency(source, sha, root):
        target = directory / source.relative_to(root)
        row = record(source, target, sha)
        if source.is_file():
            data = source.read_bytes()
        else:
            data = (plan.archive / source.relative_to(root)).read_bytes()
            if hashlib.md5(data).hexdigest() != source.stem.lower():
                raise ValueError("archive CAS does not match its md5 name")
        if raw._sha(data) != sha:
            raise ValueError("isolation dependency changed since live inspection")
        if target.exists() and raw._fingerprint(target) != sha:
            raise ValueError("isolated dependency paths collide")
        raw._atomic_bytes(target, data)
        if raw._fingerprint(target) != sha:
            raise OSError("isolated dependency reread failed")
        records.append(row)
        recovered.add(source)

    dependencies_by_source = {}
    dependents = {}
    for source in held:
        action = by_source[source]
        dependencies = (action.cas_references & held) | set(result.isolation_dependencies.get(source, {}))
        if action.kind == "chat":
            dependencies |= {a.source for a in plan.actions
                             if a.group == action.group and a.kind != "chat" and a.source in held}
        dependencies_by_source[source] = dependencies
        for dependency in dependencies:
            dependents.setdefault(dependency, set()).add(source)
    # Copy attachments before discovery markers. A failed dependency keeps its
    # chat live and prevents publishing an incomplete marker in the isolated root.
    def marker(source):
        action = by_source[source]
        return action.kind in {"chat", "range"} or bool(dependencies_by_source[source])

    for source in sorted(held, key=lambda p: (marker(p), by_source[p].kind == "chat", str(p))):
        if source in preparation_failed:
            continue  # Already reported; keep this original and its known media live.
        action = by_source[source]
        dependencies = dependencies_by_source[source]
        try:
            for path in dependencies - held - recovered:
                recover_dependency(path, result.isolation_dependencies[source][path], plan.roots[action.source_root])
            if any(path not in copied and path not in recovered for path in dependencies):
                raise OSError("引用的保留附件未能隔离")
            relative = source.relative_to(plan.roots[action.source_root])
            row = record(source, directory / relative, action.source_sha, action)
        except (OSError, ValueError) as exc:
            warn(source, exc)
            continue
        target = directory / source.relative_to(plan.roots[action.source_root])
        try:
            data = source.read_bytes()
            if raw._sha(data) != action.source_sha:
                raise ValueError("incoming changed before isolation")
            if target.exists() and raw._fingerprint(target) != action.source_sha:
                raise ValueError("isolated relative paths collide")
            raw._atomic_bytes(target, data)
            if raw._fingerprint(target) != action.source_sha:
                raise OSError("isolation reread failed")
            copied[source] = target
            records.append(row)
        except (OSError, ValueError) as exc:
            warn(source, exc)
    # The manifest is a sibling: the isolated raw root contains only original inputs.
    manifest = {"schemaVersion": 1, "rotatedAt": when, "files": records}
    try:
        raw._atomic_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    except (OSError, ValueError) as exc:
        warn(directory, exc)
        return
    # Remove discovery markers first. Failed removal leaves all their live media
    # in place too, while verified isolated copies remain available for inspection.
    for source in sorted(copied, key=lambda p: (not marker(p), str(p))):
        action = by_source[source]
        try:
            live_dependents = [path for path in dependents.get(source, ()) if path.exists()]
            if live_dependents:
                continue
            if raw._fingerprint(copied[source]) != action.source_sha or raw._fingerprint(source) != action.source_sha:
                raise OSError("isolation bytes changed before source removal")
            raw._unlink(source)
            result.isolated_sources[source] = copied[source]
        except (OSError, ValueError) as exc:
            warn(source, exc)


def refresh_isolation_report(plan, result, items):
    """Keep unresolved batches distinct; directory existence settles old batches."""
    parent = retention_root(plan.archive.parent)
    kept = []
    for item in items:
        directory = item.get("isolationDirectory")
        if directory and Path(directory).parent == parent and not Path(directory).is_dir():
            live_failures = [d.get("retainedSource") for d in item.get("details", [])
                             if d.get("retainedSource") and Path(d["retainedSource"]).is_file()
                             and not Path(d["retainedSource"]).is_relative_to(parent)]
            if not live_failures:
                continue
            item.pop("isolationDirectory", None)
        kept.append(item)
    # Apply may retain an initially-ready range. Such warnings are not present
    # in the plan's reasons and need a current item before path rewriting.
    retained = {str(path) for path in result.retained}
    for warning in result.warnings:
        source = warning.get("source")
        if source not in retained and not any(r.startswith("isolation_failed:") for r in warning.get("reasons", [])):
            continue
        matches = [item for item in kept if not item.get("isolationDirectory") and
                   (source == item.get("source") or source in item.get("sources", []))]
        if not matches:
            matches = [dict(warning, entrypoint=plan.policy)]
            kept.extend(matches)
        for item in matches:
            item["reasons"] = sorted(set(item.get("reasons", [])) | set(warning.get("reasons", [])))
    for item in kept:
        if item.get("isolationDirectory"):
            continue  # An older batch can have the same original live pathname.
        sources = set(item.get("sources", []))
        if item.get("source"):
            sources.add(item["source"])
        failures = [reason for warning in result.warnings if warning.get("source") in sources
                    for reason in warning.get("reasons", []) if reason.startswith("isolation_failed:")]
        if failures:
            item["reasons"] = sorted(set(item.get("reasons", [])) | set(failures))
        affected = {source: path for source, path in result.isolated_sources.items() if str(source) in sources}
        if not affected:
            continue
        item["isolationDirectory"] = str(result.isolation_dir)
        for detail in item.get("details", []):
            source = Path(detail["retainedSource"]) if detail.get("retainedSource") else None
            if source in result.isolated_sources:
                detail["retainedSource"] = str(result.isolated_sources[source])
        if "source" in item and Path(item["source"]) in affected:
            item["source"] = str(affected[Path(item["source"])])
    from .raw_archive import _report_item_key
    by_key = {_report_item_key(item): item for item in kept}
    for warning in result.warnings:
        if any(r.startswith("isolation_failed:") for r in warning.get("reasons", [])):
            entry = dict(warning, entrypoint=plan.policy)
            by_key.setdefault(_report_item_key(entry), entry)
    return [by_key[key] for key in sorted(by_key)]


def report_without_raw(archived_root, report_opener=None):
    """Reconcile deleted isolated batches even on an otherwise empty rotation."""
    from .raw_archive import RawIngestPlan, ArchiveIndex, ApplyResult
    from .processed_reporting import report_raw_result
    archive = Path(archived_root) / "raw"
    report_raw_result(RawIngestPlan(archive, [], "daily", ArchiveIndex(archive)),
                      ApplyResult(), report_opener=report_opener)


def isolation_report_lines(archived_root):
    parent = retention_root(archived_root)
    lines = ["## 隔离目录", "", f"隔离根：{parent}", ""]
    if not parent.is_dir():
        return lines + ["当前没有隔离子目录。", ""]
    directories = sorted(p for p in parent.iterdir() if p.is_dir())
    for directory in directories:
        lines.append(f"- `{directory}`")
    if not directories:
        lines.append("当前没有隔离子目录。")
    orphans = [p for p in parent.glob("*.manifest.json")
               if not (parent / p.name.removesuffix(".manifest.json")).is_dir()]
    if orphans:
        lines.append("孤儿 manifest（对应子目录已不存在，不阻断）："
                     + "、".join(f"`{p}`" for p in sorted(orphans)))
    return lines + [""]
