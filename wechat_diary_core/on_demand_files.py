"""Shared XML identity, exact local lookup, canonical CAS and named file exports."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote


def resolve_files_root(config_path: Path, explicit: str | None = None) -> Path:
    config = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    value = explicit or config.get("on_demand", {}).get("files_root")
    if value:
        root = Path(value).expanduser()
    else:
        account = config.get("favorites", {}).get("account_dir")
        if not account:
            raise ValueError("导出文件需要 --files-root <本地文件目录> 或 [on_demand].files_root。")
        root = Path(account).expanduser() / "msg" / "file"
    if not root.is_absolute():
        root = config_path.parent / root
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"附件源目录不存在：{root}")
    return root


def _metadata(message: dict) -> dict | None:
    from .backends.weflow_api.appmsg import _sanitize_xml
    raw = str(message.get("rawContent") or message.get("content") or "")
    # Group sender prefixes may precede the XML document.
    offset = raw.find("<")
    try:
        root = ET.fromstring(_sanitize_xml(raw[offset:] if offset >= 0 else raw))
    except (ET.ParseError, ValueError):
        local_type = int(message.get("localType") or 0)
        subtype = local_type >> 32
        if subtype not in {6, 74} and not re.search(r"<type>\s*(6|74)\s*</type>", raw):
            return None
        return {"server_id": str(message.get("serverId") or ""),
                "local_id": str(message.get("localId") or ""),
                "create_time": message.get("createTime"), "name": "无法解析的文件消息", "md5": "", "size": None}
    app = root if root.tag == "appmsg" else root.find(".//appmsg")
    if app is None or (app.findtext("type") or "").strip() not in {"6", "74"}:
        return None
    md5 = (app.findtext("appattach/md5") or app.findtext("md5") or app.findtext("appattach/filemd5") or "").lower().strip()
    size_text = (app.findtext("appattach/totallen") or "").strip()
    return {"server_id": str(message.get("serverId") or ""),
            "local_id": str(message.get("localId") or ""),
            "create_time": message.get("createTime"),
            "name": app.findtext("title") or "附件",
            "md5": md5,
            "size": int(size_text) if size_text.isdigit() else None}


def file_extension(name: str) -> str:
    """Same suffix contract as the offline backfill writer."""
    tail = re.split(r"[/\\]", str(name))[-1].rstrip(" .")
    suffix = Path(tail).suffix[1:].lower()
    return suffix if re.fullmatch(r"[a-z0-9]{1,16}", suffix) and re.search("[a-z]", suffix) else "bin"


def is_file_message(message):
    """Canonical file evidence, including API's generic other-message type."""
    kind = str(message.get("type") or "")
    content = message.get("content")
    return kind in {"文件消息", "文件"} or (kind != "文本消息" and isinstance(content, str)
        and re.match(r"^\[文件(?:[：:]|\])", content.lstrip()) is not None)


class FileResolver:
    """One lazy, read-only size index per export run; hashes only candidates."""

    def __init__(self, roots=(), *, cas_roots=(), enabled=True):
        self.roots = tuple(Path(p).resolve() for p in roots) if enabled else ()
        self.cas_roots = tuple(Path(p).resolve() for p in cas_roots) if enabled else ()
        self.enabled = enabled
        self.index = None
        self.hashes = {}
        self.scan_errors = 0
        self.total = self.acquired = self.missing = self.no_md5 = 0
        self.unchecked = 0
        self.warned = False
        self.index_seconds = 0.0

    @classmethod
    def from_config(cls, cfg, *, explicit=None, enabled=None, extra_cas=(), legacy=False):
        active = cfg.file_attachments.enabled if enabled is None else cfg.file_attachments.enabled and enabled
        if not active:
            return cls(enabled=False)
        roots = cfg.file_attachments.sources
        if explicit:
            roots = (Path(explicit),)
        if not roots:
            old_root = cfg.source.get("on_demand", {}).get("files_root") if legacy else None
            account = cfg.source.get("favorites", {}).get("account_dir")
            value = old_root or (str(Path(account) / "msg/file") if account else "")
            roots = ((cfg.base_dir / Path(value).expanduser()).resolve(),) if value else ()
        return cls(roots, cas_roots=(cfg.paths.archived / "raw/_media/files", *extra_cas), enabled=active)

    @staticmethod
    def _safe(path):
        try:
            return not path.is_symlink() and not (getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except OSError:
            return False

    def _hash(self, path):
        info = path.stat()
        signature = (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        previous = self.hashes.get(path)
        if previous is None or previous[0] != signature:
            previous = (signature, _digest(path))
            self.hashes[path] = previous
        return previous[1]

    def _build_index(self):
        import time
        started = time.perf_counter()
        self.index = {}
        def error(_):
            self.scan_errors += 1
        for root in self.roots:
            if not self._safe(root):
                continue
            for parent, dirs, names in os.walk(root, followlinks=False, onerror=error):
                dirs[:] = sorted(d for d in dirs if self._safe(Path(parent)/d))
                for name in sorted(names):
                    path = Path(parent)/name
                    try:
                        if self._safe(path) and path.is_file():
                            self.index.setdefault(path.stat().st_size, []).append(path)
                    except OSError:
                        self.scan_errors += 1
        self.index_seconds = time.perf_counter() - started

    def find(self, entry):
        if not self.enabled or not re.fullmatch(r"[0-9a-f]{32}", entry["md5"]) or entry["size"] is None:
            return None
        md5, size = entry["md5"], entry["size"]
        if not self.warned and not self.roots and not any(p.is_dir() for p in self.cas_roots):
            print("[INFO] 未配置文件来源，文件只记标签")
            self.warned = True
        # Direct shard lookup avoids scanning the archive's entire file store.
        for root in self.cas_roots:
            shard = root/md5[:2]
            if not self._safe(root) or not self._safe(shard):
                continue
            for path in sorted(shard.glob(md5+".*")):
                try:
                    if self._safe(path) and path.stat().st_size == size and self._hash(path) == md5:
                        return path
                except OSError:
                    self.scan_errors += 1
        if self.index is None:
            self._build_index()
        for path in self.index.get(size, ()):
            try:
                if self._safe(path) and path.stat().st_size == size and self._hash(path) == md5:
                    return path
            except OSError:
                self.scan_errors += 1
        return None

    def localize(self, messages, store_root):
        from .media_store import chat_media_reference, store_media_file
        entries = [_metadata(m) for m in messages]
        extensions = {}
        for entry in entries:
            if entry:
                ext = file_extension(entry["name"])
                extensions.setdefault(entry["md5"], set()).add(ext)
        result = []
        for entry in entries:
            reference = ""
            if entry:
                self.total += 1
                if not re.fullmatch(r"[0-9a-f]{32}", entry["md5"]):
                    self.no_md5 += 1
                elif not self.enabled:
                    self.unchecked += 1
                else:
                    source = self.find(entry)
                    if source is not None and store_root is not None:
                        choices = extensions[entry["md5"]] - {"bin"}
                        ext = min(choices) if choices else "bin"
                        if any(source.is_relative_to(root) for root in self.cas_roots):
                            ext = source.suffix[1:]
                        try:
                            if any(Path(store_root).resolve().is_relative_to(root) or root.is_relative_to(Path(store_root).resolve())
                                   for root in self.roots):
                                raise ValueError("附件输出与来源必须分离")
                            stored = store_media_file(source, store_root, "files", extension="."+ext,
                                                      expected_identity=(entry["md5"], entry["size"]))
                            reference = chat_media_reference(stored.relative_path)
                        except (OSError, ValueError, RuntimeError):
                            self.scan_errors += 1
                    if reference:
                        self.acquired += 1
                    else:
                        self.missing += 1
            result.append(reference)
        return result

    def summary(self, *, bulk=False):
        suffix = f"；文件导出已关闭，未检查 {self.unchecked} 条" if not self.enabled else ""
        print(f"[INFO] 文件 {self.total} 条：取到 {self.acquired}，本机没有 {self.missing}，无 MD5 {self.no_md5}{suffix}")
        if bulk and self.index is not None:
            print(f"[INFO] 文件来源索引耗时 {self.index_seconds:.3f} 秒；扫描/校验异常 {self.scan_errors} 次")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "md5").hexdigest()


def _safe_name(title: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).rstrip(" .") or "附件"
    suffix = Path(name).suffix[:20]
    if len(name) > 180:
        name = name[:180-len(suffix)] + suffix
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])", name.split('.')[0]):
        name = '_' + name
    return name


def _publish_named_file(copied: Path, destination: Path, name: str, digest: str,
                        reserved: set[str]) -> str:
    suffix = Path(name).suffix
    stem = name[:-len(suffix)] if suffix else name
    numbered = re.fullmatch(r"(.*?)\((\d+)\)", stem)
    base, first = (numbered[1], int(numbered[2])+1) if numbered else (stem, 1)
    index = 0
    while True:
        filename = name if index == 0 else f"{base}({first+index-1}){suffix}"
        folded = filename.casefold()
        if folded in {'readme.md', 'manifest.json'} or (index > 0 and folded in reserved):
            index += 1
            continue
        existing = next((path for path in destination.iterdir() if path.name.casefold() == folded), None)
        if existing is not None:
            if existing.is_file() and not existing.is_symlink() and _digest(existing) == digest:
                return existing.name
            index += 1
            continue
        target = destination / filename
        try:
            stream = target.open('xb')
        except FileExistsError:
            # A concurrent writer claimed the name; recheck rather than overwrite.
            continue
        try:
            with stream, copied.open('rb') as source:
                shutil.copyfileobj(source, stream)
            if _digest(target) != digest:
                raise OSError('附件发布后校验失败')
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return filename


def export_files(messages: list[dict], source_root: Path | None, destination: Path, *, resolver=None) -> dict:
    source_root = source_root.resolve() if source_root is not None else None
    destination = destination.resolve()
    if source_root is not None and (destination.is_relative_to(source_root) or source_root.is_relative_to(destination)):
        raise ValueError("附件输出与源目录必须分离，避免改动微信缓存或递归收取输出。")
    resolver = resolver or FileResolver((source_root,) if source_root else ())
    entries = [entry for message in messages if (entry := _metadata(message)) is not None]
    wanted = {entry["md5"] for entry in entries if re.fullmatch(r"[a-f0-9]{32}", entry["md5"])}
    destination.mkdir(parents=True, exist_ok=True)
    reserved = {_safe_name(entry['name']).casefold() for entry in entries}
    for entry in entries:
        digest = entry["md5"]
        if digest not in wanted or entry["size"] is None:
            entry["status"] = "unverifiable"
            continue
        source = resolver.find(entry)
        if source is None:
            entry["status"] = "not_found"
            continue
        handle, temporary = tempfile.mkstemp(dir=destination, prefix=".copy-")
        os.close(handle)
        try:
            shutil.copyfile(source, temporary)
            copied = Path(temporary)
            if _digest(copied) != digest or (entry["size"] is not None and copied.stat().st_size != entry["size"]):
                entry["status"] = "verification_failed"
                continue
            filename = _publish_named_file(copied, destination, _safe_name(entry['name']), digest, reserved)
            entry.update(status="exported", path=filename)
        except OSError:
            entry["status"] = "copy_failed"
        finally:
            Path(temporary).unlink(missing_ok=True)
    exported = sum(entry["status"] == "exported" for entry in entries)
    report = {"total": len(entries), "exported": exported, "unresolved": len(entries) - exported,
              "scan_errors": resolver.scan_errors, "entries": entries}
    (destination / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 文件附件", "", f"文件消息 {len(entries)} 条，已导出 {exported} 条，未取得 {len(entries)-exported} 条，扫描错误 {resolver.scan_errors}。", ""]
    labels = {"unverifiable": "消息缺少可核验身份", "not_found": "本次扫描未找到匹配字节",
              "verification_failed": "复制后内容或大小校验失败", "copy_failed": "复制失败"}
    for entry in entries:
        label = entry["name"].replace("\n", " ").replace("[", "\\[").replace("]", "\\]")
        lines.append(f"- [{label}]({quote(entry['path'])})" if entry["status"] == "exported" else f"- {label}：{labels[entry['status']]}")
    (destination / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
