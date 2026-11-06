"""Shared image identity, grouping and byte-stable group/pack contexts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
from pathlib import Path
import re
import struct
import warnings
from typing import Any, Sequence

from ..config import ImageVisionConfig
from ..emoji import render_emoji
from ..media_store import is_media_reference
from .vision_records import VisionError


LOGGER = logging.getLogger(__name__)
_missing_signal_warned = False
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
CHAT_HEAD = "聊天前后文（[发图人]=发这张图的人，[他人N]=其他人，按在本组窗口内首次出现编号；时间为当日时分）："


def text_sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _context_line(value: str) -> str:
    # Context is source material. Output-field delimiter sanitization must not
    # change a quoted message or a client's custom pat suffix.
    return re.sub(r"\s+", " ", value).strip()


def sender_key(message: dict) -> str:
    return str(message.get("senderUsername") or ("self" if int(message.get("isSend") or 0) == 1 else "unknown"))


def message_time(message: dict) -> float:
    value = message.get("createTime")
    if value:
        return float(value)
    try:
        return datetime.fromisoformat(str(message.get("formattedTime") or "")).timestamp()
    except ValueError:
        return 0


def message_day(message: dict) -> str:
    formatted = str(message.get("formattedTime") or "")
    if len(formatted) >= 10:
        return formatted[:10]
    return datetime.fromtimestamp(message_time(message)).date().isoformat()


@dataclass
class ImageItem:
    path: Path
    owner: dict[str, Any]
    index: int
    ordinal: int
    timestamp: float
    sender: str
    day: str
    sha256: str
    md5: str
    size: tuple[int, int]
    phash: str | None
    label: dict | None = None
    entry: dict | None = None
    weak_hit: bool = False
    source_session: dict | None = None

    @property
    def small(self) -> bool:
        return max(self.size) <= 300


def image_item(path: Path, owner: dict, index: int, ordinal: int, message: dict) -> ImageItem:
    global _missing_signal_warned
    if path.suffix.lower() not in IMAGE_SUFFIXES or not path.is_file():
        raise VisionError("not_image")
    try:
        from PIL import Image
        with Image.open(path) as picture:
            picture.load()
            size = picture.size
            phash = None
            try:
                import imagehash
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    phash = str(imagehash.phash(picture))
            except Exception:
                if not _missing_signal_warned:
                    LOGGER.warning("[WARN] pHash 依赖不可用，已跳过近重复信号。")
                    _missing_signal_warned = True
    except ImportError:
        if not _missing_signal_warned:
            LOGGER.warning("[WARN] Pillow 不可用，已跳过 pHash，图片尺寸退回文件头解析。")
            _missing_signal_warned = True
        size, phash = _header_size(path), None
    except (OSError, ValueError) as exc:
        raise VisionError("not_image") from exc
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
            md5.update(chunk)
    return ImageItem(path, owner, index, ordinal, message_time(message), sender_key(message),
                     message_day(message), sha.hexdigest(), md5.hexdigest(), size, phash,
                     source_session=message if owner is not message else None)


def _header_size(path: Path) -> tuple[int, int]:
    """Dependency-free type/size fallback; never infer image type from suffix."""
    data = path.read_bytes()
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
            size = struct.unpack(">II", data[16:24])
        elif data.startswith((b"GIF87a", b"GIF89a")):
            size = struct.unpack("<HH", data[6:10])
        elif data.startswith(b"BM"):
            width, height = struct.unpack("<ii", data[18:26])
            size = abs(width), abs(height)
        elif data.startswith(b"\xff\xd8"):
            offset, size = 2, (0, 0)
            while offset + 4 <= len(data):
                if data[offset] != 255:
                    break
                while data[offset] == 255:
                    offset += 1
                marker = data[offset]
                offset += 1
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    continue
                length = int.from_bytes(data[offset:offset + 2], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                    size = width, height
                    break
                if length < 2:
                    break
                offset += length
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP" and data[12:16] == b"VP8X":
            size = 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        else:
            raise VisionError("not_image")
        if min(size) <= 0:
            raise VisionError("not_image")
        return size
    except (ValueError, IndexError, struct.error) as exc:
        raise VisionError("not_image") from exc


@dataclass
class ImageGroup:
    items: list[ImageItem]
    body: str
    heading: str = CHAT_HEAD

    def context(self, number: int = 1) -> str:
        return f"【语境·组{number}】{self.heading}" + ("\n" + self.body if self.body else "")

    @property
    def context_sha256(self) -> str:
        return text_sha(self.context())


def group_chat(items: Sequence[ImageItem], messages: Sequence[dict], settings: ImageVisionConfig) -> list[ImageGroup]:
    groups: list[list[ImageItem]] = []
    for item in sorted(items, key=lambda x: (x.timestamp, x.index, x.ordinal)):
        prev = groups[-1][-1] if groups else None
        if (prev is None or settings.force_single or len(groups[-1]) >= settings.group_max
                or item.sender != prev.sender or item.day != prev.day
                or item.timestamp - prev.timestamp > settings.group_gap_minutes * 60):
            groups.append([])
        groups[-1].append(item)
    result = []
    for members in groups:
        body = chat_body(messages, members, settings)
        result.append(ImageGroup(members, body, CHAT_HEAD if settings.context_messages else "无语境。"))
    return result


def _is_image(message: dict) -> bool:
    return "图片" in str(message.get("type") or "") or any(
        is_media_reference(str(message.get(key) or ""), "images") for key in ("content", "source"))


def chat_body(messages: Sequence[dict], members: Sequence[ImageItem], settings: ImageVisionConfig) -> str:
    if not settings.context_messages:
        return ""
    first, last = members[0].index, members[-1].index
    indices = [i for i in range(max(0, first - settings.context_messages),
                               min(len(messages), last + settings.context_messages + 1))
               if message_day(messages[i]) == members[0].day]
    who = {members[0].sender: "[发图人]"}
    aliases: dict[str, str] = {}
    for i in indices:
        message = messages[i]
        name = str(message.get("senderDisplayName") or "")
        if name:
            identity = sender_key(message)
            aliases[name] = identity if name not in aliases or aliases[name] == identity else ""

    def tag(identity: str) -> str:
        if identity not in who:
            who[identity] = f"[他人{len(who)}]"
        return who[identity]

    member_map: dict[int, list[int]] = {}
    for ordinal, member in enumerate(members, 1):
        member_map.setdefault(member.index, []).append(ordinal)
    by_id = {str(m.get("platformMessageId")): m for m in messages if m.get("platformMessageId") is not None}
    lines = []
    for i in indices:
        message = messages[i]
        role = tag(sender_key(message))
        body = str(message.get("content") or "").strip()
        kind = str(message.get("type") or "")
        if i in member_map:
            body = " ".join(f"[本组图{n}]" for n in member_map[i])
        elif _is_image(message):
            body = "[图片，不在本组]"
        elif kind == "动画表情" or body in {"[表情]", "[动画表情]", "[表情包]"}:
            body = render_emoji(message)
        elif kind == "语音消息":
            if body and not message.get("transcribe_failed") and not body.startswith(("../", "media/", "转文字失败")):
                body = f"[语音：{body}]"
            else:
                body = "[语音]"
        elif kind == "视频消息":
            body = "[视频]"
        elif kind in {"系统消息", "其他消息"} and ("撤回了" in body or "拍了拍" in body):
            retract = re.fullmatch(r'["“][^"”]+["”]\s*撤回了一条消息', body)
            pat = re.fullmatch(r'["“][^"”]+["”]\s*拍了拍\s*(?:["“]([^"”]+)["”]|(我))(.*)', body)
            if retract:
                body = role + " 撤回了一条消息"
            elif pat:
                target = "我" if pat.group(2) else tag(aliases.get(pat.group(1)) or "display:" + pat.group(1))
                body = role + " 拍了拍 " + target + pat.group(3)
            else:
                body = "[系统提示]"
        elif kind not in {"文本消息", "引用消息", "系统消息", "其他消息", ""}:
            body = f"[{kind}]"
        quote = message.get("quotedContent")
        if not quote and message.get("replyToMessageId") is not None:
            target = by_id.get(str(message["replyToMessageId"]))
            if target:
                quote = "[图片]" if _is_image(target) else target.get("content")
        if isinstance(quote, str) and quote:
            body += f"（引用：{_context_line(quote)[:120]}）"
        stamp = str(message.get("formattedTime") or "")
        hhmm = stamp[11:16] if len(stamp) >= 16 else datetime.fromtimestamp(message_time(message)).strftime("%H:%M")
        label = role if settings.anonymize_speakers else str(message.get("senderDisplayName") or sender_key(message))
        lines.append(f"{hhmm} {label} {_context_line(body)}")
    return "\n".join(lines)


def moment_body(post: dict, settings: ImageVisionConfig) -> str:
    body = str(post.get("contentDesc") or "").strip() or "（无正文）"
    if settings.include_moment_comments:
        who: dict[str, int] = {}
        for comment in post.get("comments") or []:
            identity = str(comment.get("username") or comment.get("nickname") or "unknown")
            who.setdefault(identity, len(who) + 1)
            body += f"\n[他人{who[identity]}]：{_context_line(str(comment.get('content') or ''))}"
    return body.replace("\r\n", "\n").replace("\r", "\n")


def group_moments(items: Sequence[ImageItem], posts: Sequence[dict], settings: ImageVisionConfig) -> list[ImageGroup]:
    groups = []
    for index, post in sorted(enumerate(posts), key=lambda pair: message_time(pair[1])):
        members = sorted((x for x in items if x.index == index), key=lambda x: x.ordinal)
        limit = 1 if settings.force_single else settings.group_max
        for start in range(0, len(members), limit):
            groups.append(ImageGroup(members[start:start + limit], moment_body(post, settings),
                                     "本条动态正文（[发图人] 发布）："))
    return groups


def make_packs(groups: Sequence[ImageGroup], maximum: int) -> list[list[ImageGroup]]:
    packs: list[list[ImageGroup]] = []
    current: list[ImageGroup] = []
    weight = 0
    for group in groups:
        cost = sum(2 if x.label and x.label.get("text") == "多" else 1 for x in group.items)
        if current and weight + cost > maximum:
            packs.append(current)
            current, weight = [], 0
        current.append(group)
        weight += cost
    if current:
        packs.append(current)
    return packs


def pack_text(groups: Sequence[ImageGroup]) -> str:
    blocks, mapping = [], []
    count = 0
    for number, group in enumerate(groups, 1):
        blocks.append(group.context(number))
        size = len(group.items)
        if size == 1:
            mapping.append(f"组{number} 的本组图1 = 图{count + 1}")
        else:
            mapping.append(f"组{number} 的本组图1…{size} = 图{count + 1}…图{count + size}")
        count += size
    return "\n".join(blocks) + f"\n本次共 {count} 张，附件顺序为 图1…图{count}；" + "，".join(mapping) + "。"
