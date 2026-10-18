"""Public visual annotation facade, shared by all export entrypoints."""
from __future__ import annotations
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..media_store import is_media_reference
from .vision_prompts import PROMPT_VERSION, SYSTEM_PROMPT
from .vision_records import VisionError, VisionBudgetExhausted, normalize_description
from .vision_service import VisionService
from .vision_transport import (VisionTransport, VisionResponse, CheapApiTransport, CodexTransport,
    MAX_IMAGE_BYTES, response_failed as _response_failed)
from .vision_units import (ImageItem, ImageGroup, image_item, group_chat, group_moments, chat_body,
    moment_body, sender_key, message_day, CHAT_HEAD)

Message = dict[str, Any]
VisionDescriber = VisionTransport
# Retained import names; the old v2 tails are never sent in img-v4.
CHAT_FIXED_USER_INSTRUCTION = ""
MOMENT_FIXED_USER_INSTRUCTION = ""


class CheapApiVisionDescriber(CheapApiTransport):
    def __init__(self, settings, cache_root=None, **kwargs):
        super().__init__(settings, **kwargs)


CodexVisionDescriber = CodexTransport


def session_is_skipped(session: Mapping, messages: Sequence, skip_usernames: Sequence[str]) -> bool:
    skipped = {str(value).strip() for value in skip_usernames if str(value).strip()}
    candidates = {str(session.get(key) or "").strip() for key in ("username", "wxid", "displayName", "nickname", "remark")}
    candidates.update(sender_key(message) for message in messages)
    return bool(skipped & candidates)


def _looks_like_image(value: str) -> bool:
    return is_media_reference(value, "images")


def build_chat_context(messages, image_index, settings) -> str:
    m = messages[image_index]
    item = ImageItem(Path(), m, image_index, 0, 0, sender_key(m), message_day(m), "", "", (1, 1), None)
    return ImageGroup([item], chat_body(messages, [item], settings),
        CHAT_HEAD if settings.context_messages else "无语境。").context()


def build_moment_context(post, settings) -> str:
    return ImageGroup([], moment_body(dict(post), settings), "本条动态正文（[发图人] 发布）：").context()


def _collect(messages, getter):
    items = []
    for index, message in enumerate(messages):
        seen = set()
        for ordinal, (path, owner) in enumerate(getter(message)):
            identity = (str(path.resolve()), id(owner))
            if identity in seen:
                continue
            seen.add(identity)
            try:
                if path.stat().st_size <= MAX_IMAGE_BYTES:
                    items.append(image_item(path, owner, index, ordinal, message))
            except (VisionError, OSError, ValueError):
                continue
    return items


def _service(settings, cache_root, describer, label_settings, labeler):
    transport = describer or (CodexVisionDescriber(settings) if settings.backend == "codex"
                              else CheapApiVisionDescriber(settings, cache_root))
    return VisionService(settings, cache_root, transport=transport, label_settings=label_settings, labeler=labeler)


def annotate_vision_descriptions(messages, base_dir, settings, cache_root, image_path_getter,
        *, describer=None, label_settings=None, labeler=None, session=None):
    if not settings.enabled or session_is_skipped(session or {}, messages, settings.skip_usernames):
        return list(messages)
    items = _collect(messages, lambda m: ((p, m) for p in image_path_getter(m, base_dir)
                     if str(m.get("type") or "") != "动画表情"))
    if items:
        _service(settings, cache_root, describer, label_settings, labeler).process(
            items, lambda eligible: group_chat(eligible, messages, settings), session or {})
    return list(messages)


def annotate_moment_vision(posts, media_root, settings, cache_root, *, describer=None, label_settings=None, labeler=None):
    if not settings.enabled:
        return list(posts)
    selected = [post for post in posts if not session_is_skipped(post, [], settings.skip_usernames)]
    def getter(message):
        for media in message.get("media") or []:
            if isinstance(media, dict):
                ref = str(media.get("localPath") or "")
                if ref and "/videos/" not in ref.replace("\\", "/"):
                    yield media_root / ref, media
    items = _collect(selected, getter)
    if items:
        _service(settings, cache_root, describer, label_settings, labeler).process(
            items, lambda eligible: group_moments(eligible, selected, settings), {})
    return list(posts)
