"""Optional, MD5-verified local file attachments; canonical messages stay unchanged."""
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
from .backends.weflow_api.appmsg import _sanitize_xml


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
    if app is None or app.findtext("type") not in {"6", "74"}:
        return None
    md5 = (app.findtext("appattach/md5") or app.findtext("md5") or app.findtext("appattach/filemd5") or "").lower().strip()
    size_text = (app.findtext("appattach/totallen") or "").strip()
    return {"server_id": str(message.get("serverId") or ""),
            "local_id": str(message.get("localId") or ""),
            "create_time": message.get("createTime"),
            "name": app.findtext("title") or "附件",
            "md5": md5,
            "size": int(size_text) if size_text.isdigit() else None}


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "md5").hexdigest()


def export_files(messages: list[dict], source_root: Path, destination: Path) -> dict:
    source_root = source_root.resolve()
    destination = destination.resolve()
    if destination.is_relative_to(source_root) or source_root.is_relative_to(destination):
        raise ValueError("附件输出与源目录必须分离，避免改动微信缓存或递归收取输出。")
    entries = [entry for message in messages if (entry := _metadata(message)) is not None]
    wanted = {entry["md5"] for entry in entries if re.fullmatch(r"[a-f0-9]{32}", entry["md5"])}
    sizes = {entry["size"] for entry in entries if entry["md5"] in wanted and entry["size"] is not None}
    unknown_size = any(entry["md5"] in wanted and entry["size"] is None for entry in entries)
    found: dict[str, Path] = {}
    scan_errors = 0
    def onerror(exc):
        nonlocal scan_errors
        scan_errors += 1
    def traversable(path):
        try:
            return not path.is_symlink() and not (getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except OSError as exc:
            onerror(exc)
            return False
    # Do not follow junctions/symlinks outside the selected source.
    if wanted:
        for parent, dirs, names in os.walk(source_root, onerror=onerror, followlinks=False):
            dirs[:] = [name for name in dirs if traversable(Path(parent) / name)]
            for name in names:
                path = Path(parent) / name
                try:
                    if path.is_symlink() or not path.resolve().is_relative_to(source_root.resolve()):
                        continue
                    if not unknown_size and path.stat().st_size not in sizes:
                        continue
                    digest = _digest(path)
                    if digest in wanted:
                        found.setdefault(digest, path)
                except OSError:
                    scan_errors += 1
            if wanted <= found.keys():
                break
    destination.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        digest = entry["md5"]
        if digest not in wanted:
            entry["status"] = "unverifiable"
            continue
        source = found.get(digest)
        if source is None:
            entry["status"] = "not_found"
            continue
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", entry["name"]).strip(" .")[:100] or "附件"
        filename = f"{digest}-{safe}"
        handle, temporary = tempfile.mkstemp(dir=destination, prefix=".copy-")
        os.close(handle)
        try:
            shutil.copyfile(source, temporary)
            copied = Path(temporary)
            if _digest(copied) != digest or (entry["size"] is not None and copied.stat().st_size != entry["size"]):
                entry["status"] = "verification_failed"
                continue
            os.replace(copied, destination / filename)
            entry.update(status="exported", path=filename)
        except OSError:
            entry["status"] = "copy_failed"
        finally:
            Path(temporary).unlink(missing_ok=True)
    exported = sum(entry["status"] == "exported" for entry in entries)
    report = {"total": len(entries), "exported": exported, "unresolved": len(entries) - exported,
              "scan_errors": scan_errors, "entries": entries}
    (destination / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 文件附件", "", f"文件消息 {len(entries)} 条，已导出 {exported} 条，未取得 {len(entries)-exported} 条，扫描错误 {scan_errors}。", ""]
    labels = {"unverifiable": "消息缺少可核验身份", "not_found": "本次扫描未找到匹配字节",
              "verification_failed": "复制后内容或大小校验失败", "copy_failed": "复制失败"}
    for entry in entries:
        label = entry["name"].replace("\n", " ").replace("[", "\\[").replace("]", "\\]")
        lines.append(f"- [{label}]({quote(entry['path'])})" if entry["status"] == "exported" else f"- {label}：{labels[entry['status']]}")
    (destination / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
