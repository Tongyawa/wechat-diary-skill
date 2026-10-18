"""Strict response validation and deterministic img-v4 rendering."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any

from .vision_prompts import OUTPUT_SCHEMA


class VisionError(RuntimeError):
    """An isolated visual request failed; never cache it as a success."""


class VisionBudgetExhausted(VisionError):
    pass


class ProviderUnavailable(VisionError):
    pass


MOTIVE = re.compile(r"发图人|对方|他人\d*|语境|回应|吐槽|(像是在|用以|用来|为了)(展示|表达)")


def parse_json(text: str) -> Any:
    try:
        first, last = text.index("{"), text.rindex("}")
        return json.loads(text[first:last + 1], parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (ValueError, TypeError) as exc:
        raise VisionError("vision_invalid_json") from exc


def validate(value: Any, schema: dict) -> None:
    kind = schema.get("type")
    valid = {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: type(value) is int,
    }
    if kind in valid and not valid[kind]():
        raise VisionError("vision_invalid_json: type")
    if "enum" in schema and value not in schema["enum"]:
        raise VisionError("vision_invalid_json: enum")
    if kind == "object":
        props = schema.get("properties", {})
        if not set(schema.get("required", ())).issubset(value):
            raise VisionError("vision_invalid_json: missing fields")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise VisionError("vision_invalid_json: extra fields")
        for key in value.keys() & props.keys():
            validate(value[key], props[key])
    elif kind == "array":
        for item in value:
            validate(item, schema["items"])


def parse_records(text: str, count: int) -> list[dict]:
    body = parse_json(text)
    validate(body, OUTPUT_SCHEMA)
    items = body["images"]
    if len(items) != count or sorted(x["slot"] for x in items) != list(range(1, count + 1)):
        raise VisionError("vision_invalid_json: N/slot mismatch")
    return sorted(items, key=lambda x: x["slot"])


def clean_field(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("｜", "|")).strip()


def normalize_description(value: str, max_chars: int) -> str:
    text = clean_field(value)
    return text[:max_chars - 1] + "…" if max_chars > 0 and len(text) > max_chars else text


def render_record(record: dict, max_chars: int = 1000) -> str:
    pieces = [record.get("kind", ""), record.get("gist", "")]
    for key, prefix, separator in (("text", "文字：", " / "), ("people", "人物：", "；")):
        values = [clean_field(x) for x in record.get(key, []) if clean_field(x)]
        if values:
            pieces.append(prefix + separator.join(values))
    pieces.append(record.get("detail", ""))
    unread = [clean_field(x) for x in record.get("unread", []) if clean_field(x)]
    if unread:
        pieces.append("未转写：" + "；".join(unread))
    text = "｜".join(clean_field(x) for x in pieces if clean_field(x))
    return text[:max_chars - 1] + "…" if max_chars > 0 and len(text) > max_chars else text


def finish_record(record: dict, size: tuple[int, int], max_chars: int, rewrite=None) -> tuple[dict, dict]:
    result = deepcopy(record)
    checks = dict(gist_motive=False, gist_rewritten=False, uncertain_heavy=False,
                  over_budget=False, detail_cut_chars=0, orientation_mismatch=False, context_bleed=[])
    hit = MOTIVE.search(result["gist"])
    if hit:
        try:
            replacement = rewrite(result["gist"], hit.group()) if rewrite else ""
        except Exception:
            replacement = ""
        replacement = clean_field(replacement)
        if replacement and not MOTIVE.search(replacement):
            result["gist"] = replacement
            checks["gist_rewritten"] = True
        else:
            checks["gist_motive"] = True
    lines = [line for line in result["text"] if not line.startswith("[")]
    q = sum("?" in line or "？" in line for line in lines)
    checks["uncertain_heavy"] = q >= 4 and q > len(lines) / 2
    width, height = size
    expected = "square" if width == height else "landscape" if width > height else "portrait"
    checks["orientation_mismatch"] = result["orientation"] != expected
    budget = max(width * height / 750, 150)
    original = result["detail"]
    sentences = re.findall(r"[^。！？；]+[。！？；]?", original)
    while len(render_record(result, 0)) > budget and sentences:
        sentences.pop()
        result["detail"] = "".join(sentences)
    checks["detail_cut_chars"] = len(original) - len(result["detail"])
    checks["over_budget"] = len(render_record(result, 0)) > budget
    return result, checks


def norm_kind(kind: str) -> str:
    for keys, value in ((("表情", "梗"), "表情包梗图"), (("插画", "二次元"), "插画二次元"),
                        (("拼图", "对照"), "拼图对照图"), (("聊天",), "聊天截图"),
                        (("应用", "网页"), "应用网页截图"), (("文档",), "文档图"),
                        (("截帧", "视频"), "视频截帧"), (("照片",), "照片")):
        if any(key in kind for key in keys):
            return value
    return "其他"


def shift_suspect(records: list[dict], labels: list[dict | None]) -> bool:
    formal = [norm_kind(x["kind"]) for x in records]
    coarse = [x.get("kind") if x else None for x in labels]
    mismatches = [i for i, value in enumerate(coarse) if value and formal[i] != value]
    moved = [i for i in mismatches if (i > 0 and formal[i] == coarse[i - 1])
             or (i + 1 < len(coarse) and formal[i] == coarse[i + 1])]
    return len(mismatches) >= 2 and len(moved) >= 2


def fragments(text: str) -> set[str]:
    text = re.sub(r"^\d\d:\d\d \[[^\]]+\] ", "", text, flags=re.M)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    result = set()
    for run in re.findall(r"[\u4e00-\u9fff]{3,}", text):
        for n in range(3, min(len(run), 8) + 1):
            result.update(run[i:i + n] for i in range(len(run) - n + 1))
    result.update(word.lower() for word in re.findall(r"[A-Za-z0-9]{4,}", text))
    return result


def context_bleed(record: dict, group_index: int, bodies: list[str]) -> list[str]:
    frags = [fragments(x) for x in bodies]
    others = set().union(*(x for i, x in enumerate(frags) if i != group_index)) - frags[group_index]
    hay = " ".join([record["gist"], " ".join(record["text"]), record["context_link"]]).lower()
    hits = {word for word in others if word.lower() in hay}
    return sorted((word for word in hits if not any(word != x and word in x for x in hits)),
                  key=lambda word: (-len(word), word))[:8]
