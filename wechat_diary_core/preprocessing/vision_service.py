"""Shared orchestration: label, gate, group, cache, pack, validate, render."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import logging
from pathlib import Path
import subprocess
import tempfile

from ..config import ImageLabelConfig, ImageVisionConfig
from .vision_prompts import (PROMPT_VERSION, CONTEXT_FORMAT, SYSTEM_PROMPT, OUTPUT_SCHEMA,
                             LABEL_VERSION, LABEL_PROMPT, LABEL_SCHEMA)
from .vision_records import (VisionError, VisionBudgetExhausted, ProviderUnavailable, parse_json,
                             validate, parse_records, render_record, finish_record, shift_suspect, context_bleed)
from .vision_store import VisionStore, single_flight
from .vision_transport import CheapApiTransport, CodexTransport, response_failed
from .vision_units import text_sha, make_packs, pack_text

LOGGER = logging.getLogger(__name__)
LABEL_MODEL = "gpt-6-luna/low"


class VisionService:
    def __init__(self, settings: ImageVisionConfig, cache_root: Path, *, transport=None,
                 label_settings=None, labeler=None):
        self.settings = settings
        root = settings.cache_dir or cache_root / "_image-descriptions"
        if not root.resolve().is_relative_to(cache_root.resolve()):
            raise ValueError("image vision cache_dir 必须位于 archived 根下")
        self.store = VisionStore(root)
        self.transport = transport or (CodexTransport(settings) if settings.backend == "codex" else CheapApiTransport(settings))
        self.label_settings = label_settings or ImageLabelConfig()
        label_config = replace(settings, backend="codex", model="gpt-6-luna", effort="low",
                               timeout_sec=self.label_settings.timeout_sec)
        self.labeler = labeler or CodexTransport(label_config)
        self.counts = Counter()
        self.provider_unavailable = False

    @property
    def model(self):
        return self.settings.model + "/" + self.settings.effort

    def labels(self, items):
        if not self.label_settings.enabled:
            return
        unique = {item.sha256: item for item in items}
        pending = []
        for item in unique.values():
            item.label = self.store.label(item.sha256, LABEL_MODEL)
            if item.label is None:
                pending.append(item)
        for start in range(0, len(pending), self.label_settings.batch_size):
            batch = pending[start:start + self.label_settings.batch_size]
            try:
                from PIL import Image
                with tempfile.TemporaryDirectory(prefix="diary-label-") as directory:
                    paths = []
                    for slot, item in enumerate(batch, 1):
                        path = Path(directory) / f"image-{slot:03}.png"
                        with Image.open(item.path) as original:
                            thumb = original.convert("RGBA")
                            thumb.thumbnail((self.label_settings.thumbnail_px,) * 2)
                            thumb.save(path)
                        paths.append(path)
                    response = self.labeler.request(paths, f"本批共 {len(batch)} 张。", LABEL_PROMPT, LABEL_SCHEMA)
                    if response_failed(response):
                        raise VisionBudgetExhausted("label_empty_or_length")
                    body = parse_json(response.content)
                    validate(body, LABEL_SCHEMA)
                    values = body["items"]
                    if (len(values) != len(batch) or sorted(x["slot"] for x in values) != list(range(1, len(batch) + 1))
                            or any(len(x["anchor"]) > 12 for x in values)):
                        raise VisionError("label_count_slot_anchor")
                    for item, value in zip(batch, sorted(values, key=lambda x: x["slot"])):
                        item.label = value
                        self.store.put_label(item.sha256, LABEL_MODEL, value)
            except (VisionError, OSError, ValueError, ImportError, subprocess.TimeoutExpired):
                self.counts["label_failed"] += len(batch)
                self.store.diagnostic("label_failed", reason="label_failed")
        for item in items:
            item.label = unique[item.sha256].label

    def eligible(self, items, session):
        result = []
        for item in items:
            identity = item.source_session or session
            allowed = bool({x.strip() for x in self.settings.allow_usernames if x.strip()} &
                {str(identity.get(k) or "").strip() for k in ("username", "wxid", "displayName", "nickname", "remark")})
            item.owner["image_signals"] = self.store.signal(item)
            if (item.label and item.label["sensitive"] != "无" and not allowed
                    and item.sha256 not in self.settings.allow_image_sha256):
                label = item.label
                item.owner["image_vision_inline"] = f"{label['kind']}｜含{label['sensitive']}，未转写"
                item.owner["image_vision_sensitive"] = label["sensitive"]
                item.owner.pop("image_vision", None)
                self.counts["sensitive:" + label["sensitive"]] += 1
            else:
                result.append(item)
        return result

    def attach(self, item, entry, weak):
        item.entry, item.weak_hit = entry, weak
        for key in ("weak_hit", "image_vision_pending", "image_vision_sensitive"):
            item.owner.pop(key, None)
        item.owner["image_vision"] = entry["record"]
        item.owner["image_vision_inline"] = render_record(entry["record"], self.settings.max_inline_chars)
        item.owner["image_vision_checks"] = entry.get("checks", {})
        if weak:
            item.owner["weak_hit"] = True
            item.owner["image_vision_pending"] = dict(image_sha256=item.sha256,
                context_sha256=item.owner.get("image_context_sha256"), reason="weak_hit")
            self.counts["weak_hit"] += 1

    def process(self, items, make_groups, session):
        # Process-local single-flight; cross-process locks are a runner concern.
        with single_flight(str(self.store.root) + ":service"):
            try:
                self.model
            except (ProviderUnavailable, OSError, subprocess.TimeoutExpired):
                self.counts["provider_unavailable"] += 1
                self.counts["fallback"] += len(items)
                LOGGER.warning("[WARN] 图片视觉接入不可用；请检查 backend、codex_command 或 cheap-api 配置，已降级到 OCR 或图片占位。")
                return self.counts
            self.labels(items)
            groups = make_groups(self.eligible(items, session))
            pending = []
            for group in groups:
                needs_call = False
                for item in group.items:
                    item.owner["image_context_sha256"] = group.context_sha256
                    entry, weak = self.store.lookup(item.sha256, group.context_sha256, self.model)
                    if entry:
                        self.attach(item, entry, weak)
                    else:
                        needs_call = True
                if needs_call:
                    pending.append(group)
            maximum = self.settings.pack_max or (8 if self.settings.backend == "codex" and self.settings.model == "gpt-6.1-sol" else 1)
            for pack in make_packs(pending, maximum):
                if self.provider_unavailable:
                    self.counts["fallback"] += sum(item.entry is None for group in pack for item in group.items)
                else:
                    self.transcribe(pack)
        if self.counts["fallback"] or self.counts["label_failed"]:
            LOGGER.warning("[WARN] 图片视觉处理：%s 张降级，%s 张打标失败；已保留 OCR 或图片占位。",
                           self.counts["fallback"], self.counts["label_failed"])
        return self.counts

    def transcribe(self, groups):
        flat = [(gi, group, item) for gi, group in enumerate(groups) for item in group.items]
        for _, group, item in flat:
            if item.entry is None:
                cached, weak = self.store.lookup(item.sha256, group.context_sha256, self.model)
                if cached:
                    self.attach(item, cached, weak)
        if all(item.entry is not None for _, _, item in flat):
            return
        if self.provider_unavailable:
            self.counts["fallback"] += sum(item.entry is None for _, _, item in flat)
            return
        paths = [item.path for _, _, item in flat]
        pack_id = text_sha("".join(item.sha256 for _, _, item in flat))
        records, raw = None, ""
        retry_budget = False
        for attempt in range(2 if len(groups) == 1 else 1):
            try:
                budget = self.settings.empty_retry_max_tokens if retry_budget else self.settings.max_tokens
                response = self.transport.request(paths, pack_text(groups), SYSTEM_PROMPT, OUTPUT_SCHEMA, max_tokens=budget)
                if response_failed(response):
                    raise VisionBudgetExhausted("vision_empty_or_length")
                raw = response.content
                records = parse_records(raw, len(flat))
                if len(groups) >= 2 and shift_suspect(records, [item.label for _, _, item in flat]):
                    self.counts["pack_shift_suspect"] += 1
                    self.store.diagnostic("pack_shift_suspect", pack_id=pack_id, pack_size=len(flat),
                        group_sizes=[len(g.items) for g in groups], slots=list(range(1, len(flat) + 1)))
                    records = None
                if records is not None:
                    break
            except VisionBudgetExhausted:
                records = None
                self.counts["budget_exhausted"] += 1
                self.store.diagnostic("failed", pack_id=pack_id, pack_size=len(flat), reason="budget_exhausted", attempt=attempt + 1)
                if self.settings.provider.casefold() != "jisuan" or "qwen" not in self.settings.model.casefold():
                    break
                retry_budget = True
            except ProviderUnavailable:
                self.provider_unavailable = True
                self.counts["provider_unavailable"] += 1
                break
            except (VisionError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                records = None
                reason = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "invalid_response"
                self.counts[reason] += 1
                self.store.diagnostic("failed", pack_id=pack_id, pack_size=len(flat), reason=reason, attempt=attempt + 1)
        if records is None:
            if len(groups) >= 2 and not self.provider_unavailable:
                self.counts["pack_split"] += 1
                for group in groups:
                    self.transcribe([group])
            else:
                self.counts["fallback"] += sum(item.entry is None for _, _, item in flat)
            return

        def rewrite(gist, hit):
            prompt = f"原 gist：{gist}\n命中片段：{hit}\n只重写这一句 gist：保留画面是什么和梗，删去发图动机，不出现上述词。只返回一行字符串。"
            response = self.transport.request([], prompt, "你是 gist 校订器。", None)
            return "" if response_failed(response) else response.content

        for (gi, group, item), record in zip(flat, records):
            if item.entry is not None:  # Full-group resend must not overwrite hits.
                continue
            cached, weak = self.store.lookup(item.sha256, group.context_sha256, self.model)
            if cached and not weak:
                self.attach(item, cached, False)
                continue
            if not record["gist"].strip():
                if cached:
                    self.attach(item, cached, weak)
                    continue
                self.counts["fallback"] += 1
                continue
            bleed = context_bleed(record, gi, [g.body for g in groups]) if len(groups) >= 2 else []
            result, checks = finish_record(record, item.size, self.settings.max_inline_chars, rewrite)
            checks["context_bleed"] = bleed
            self.counts.update(key for key, value in checks.items() if value and key != "detail_cut_chars")
            entry = dict(record=result, raw_output=raw, checks=checks,
                unit=dict(group_size=len(group.items), pack_size=len(flat), slot=record["slot"]),
                pack_id=pack_id, context_format=CONTEXT_FORMAT,
                label=dict(item.label, label_version=LABEL_VERSION, model=LABEL_MODEL) if item.label else None,
                image_sha256=item.sha256, image_md5=item.md5, image_size=list(item.size),
                context_sha256=group.context_sha256, prompt_version=PROMPT_VERSION,
                provider=self.settings.backend if self.settings.backend == "codex" else self.settings.provider,
                model=self.model, created_at=datetime.now(timezone.utc).isoformat())
            self.store.put(entry)
            self.store.diagnostic("success", pack_id=pack_id, pack_size=len(flat), group_sizes=[len(g.items) for g in groups],
                slots=[record["slot"]], checks={key: bool(value) for key, value in checks.items()},
                detail_chars=len(record["detail"]), render_detail_chars=len(result["detail"]))
            self.attach(item, entry, False)
            self.counts["transcribed"] += 1
