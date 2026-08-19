from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
import hashlib
import json
import logging
import re
import subprocess
import sys
import threading

from ..config import ImageVisionConfig


LOGGER = logging.getLogger(__name__)
PROMPT_VERSION = "img-v1"
SYSTEM_PROMPT = """你是图片内容转写器。你的输出会被直接嵌入一份微信聊天记录的文本流，供后续的日记生成与个人画像蒸馏使用。

1. 只输出转写内容本身。不要任何前缀、开场白、结语或元评论。禁止出现「这张图片」「图中显示」「可以看到」「这是一张」之类的措辞。
2. 输出必须是单行纯文本。不得换行，不得使用 markdown 标记、列表、标题或引号包裹整段。
3. 图中所有可读文字必须完整转出，保持原文原样，不翻译、不改写、不摘要。文字是最高优先级的信息。
4. 在文字之外，简要说明画面是什么：图片类型（手机截图／照片／表情包／文档／示意图等）、主体对象、场景、正在发生的事。
5. 只描述看得见的。不推测意图、不判断情绪、不评价好坏、不补充画面之外的知识。看不清就写看不清。
6. 人物只按画面可见特征描述（例如「两个人在餐桌前」）。不猜测身份、年龄、职业、关系。
7. 长度控制在 200 字以内。文字特别多的截图（长聊天记录、长文档）可放宽到 400 字，优先保证文字完整。
8. 图片无实质内容（纯色、损坏、无法辨认）时，只输出四个字：无法识别"""
FIXED_USER_INSTRUCTION = "以上是这张图出现时的对话片段，仅供理解语境。现在转写这张图片。"
MAX_IMAGE_BYTES = 7 * 1024 * 1024
Message = dict[str, Any]


class VisionError(RuntimeError):
    pass


class VisionBudgetExhausted(VisionError):
    pass


class VisionDescriber(Protocol):
    def describe(self, image_path: Path, context_text: str) -> str: ...


@dataclass(frozen=True)
class VisionResponse:
    content: str
    finish_reason: str


Runner = Callable[[Sequence[str], str, float], subprocess.CompletedProcess[bytes]]


class CheapApiVisionDescriber:
    """Vision adapter that invokes the credential-isolating cheap-api CLI."""

    def __init__(
        self,
        settings: ImageVisionConfig,
        cache_root: Path,
        *,
        runner: Runner | None = None,
        api_script: Path | None = None,
    ) -> None:
        self.settings = settings
        self.cache_root = settings.cache_dir or cache_root / "_image-descriptions"
        self._runner = runner or _run_subprocess
        self._api_script = api_script
        self._inflight_lock = threading.Lock()
        self._inflight: dict[str, Future[str]] = {}

    def describe(self, image_path: Path, context_text: str) -> str:
        if image_path.stat().st_size > MAX_IMAGE_BYTES:
            raise VisionError(f"图片超过 7 MiB：{image_path.name}")

        image_sha = _sha256_file(image_path)
        context_sha = _sha256_text(context_text)
        cached = self._read_cache(image_sha, context_sha)
        if cached is not None:
            return cached

        flight_key = "\0".join((image_sha, context_sha, PROMPT_VERSION, self.settings.model))
        with self._inflight_lock:
            future = self._inflight.get(flight_key)
            if future is None:
                future = Future()
                self._inflight[flight_key] = future
                owner = True
            else:
                owner = False
        if not owner:
            return future.result()

        try:
            response = self._call(image_path, context_text, self.settings.max_tokens)
            if _response_failed(response):
                if _uses_total_token_budget(self.settings) and (
                    response.finish_reason == "length" or not response.content.strip()
                ):
                    response = self._call(
                        image_path,
                        context_text,
                        self.settings.empty_retry_max_tokens,
                    )
                if _response_failed(response):
                    raise VisionBudgetExhausted(
                        f"视觉响应不可用（finish_reason={response.finish_reason or 'unknown'}）"
                    )

            description = normalize_description(response.content, self.settings.max_inline_chars)
            if not description or description == "无法识别":
                raise VisionError("视觉模型未返回可用描述")
            self._write_cache(image_sha, context_sha, description)
            future.set_result(description)
            return description
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._inflight_lock:
                self._inflight.pop(flight_key, None)

    def _call(self, image_path: Path, context_text: str, max_tokens: int) -> VisionResponse:
        api_script = self._api_script or _locate_api_script(self._runner)
        prompt = f"{context_text}\n{FIXED_USER_INSTRUCTION}" if context_text else FIXED_USER_INSTRUCTION
        command = [
            sys.executable,
            str(api_script),
            "--provider",
            self.settings.provider,
            "--model",
            self.settings.model,
            "--system",
            SYSTEM_PROMPT,
            "--image",
            str(image_path),
            "--max-tokens",
            str(max_tokens),
            "--timeout",
            str(self.settings.timeout_sec),
            "--json",
        ]
        result = self._runner(command, prompt, self.settings.timeout_sec + 15)
        stdout = _decode_output(result.stdout)
        stderr = _decode_output(result.stderr)
        if result.returncode != 0:
            raise VisionError(stderr.strip() or f"cheap-api 退出码 {result.returncode}")
        try:
            body = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise VisionError("cheap-api 返回了不可解析的 JSON") from exc
        return _parse_response(body)

    def _cache_path(self, image_sha: str, context_sha: str) -> Path:
        identity = "\0".join((image_sha, context_sha, PROMPT_VERSION, self.settings.model))
        key = _sha256_text(identity)
        return self.cache_root / key[:2] / f"{key}.json"

    def _read_cache(self, image_sha: str, context_sha: str) -> str | None:
        path = self._cache_path(image_sha, context_sha)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        expected = {
            "image_sha256": image_sha,
            "context_sha256": context_sha,
            "prompt_version": PROMPT_VERSION,
            "model": self.settings.model,
        }
        if not all(entry.get(key) == value for key, value in expected.items()):
            return None
        description = normalize_description(str(entry.get("description") or ""), self.settings.max_inline_chars)
        return description or None

    def _write_cache(self, image_sha: str, context_sha: str, description: str) -> None:
        path = self._cache_path(image_sha, context_sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "description": description,
            "image_sha256": image_sha,
            "context_sha256": context_sha,
            "prompt_version": PROMPT_VERSION,
            "provider": self.settings.provider,
            "model": self.settings.model,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def annotate_vision_descriptions(
    messages: Sequence[Message],
    base_dir: Path,
    settings: ImageVisionConfig,
    cache_root: Path,
    image_path_getter: Callable[[Message, Path], list[Path]],
    *,
    describer: VisionDescriber | None = None,
) -> list[Message]:
    if not settings.enabled:
        return list(messages)

    worker = describer or CheapApiVisionDescriber(settings, cache_root)
    tasks: list[tuple[int, int, Path, str]] = []
    for index, message in enumerate(messages):
        paths = [path for path in image_path_getter(message, base_dir) if path.exists()]
        for ordinal, path in enumerate(paths):
            tasks.append((index, ordinal, path, build_chat_context(messages, index, settings)))
    if not tasks:
        return list(messages)

    descriptions: dict[int, dict[int, str]] = {}
    failures = 0
    provider_unavailable = False
    with ThreadPoolExecutor(max_workers=settings.concurrency) as pool:
        futures = {
            pool.submit(worker.describe, path, context): (index, ordinal)
            for index, ordinal, path, context in tasks
        }
        for future in as_completed(futures):
            index, ordinal = futures[future]
            try:
                description = future.result()
            except Exception as exc:
                failures += 1
                if not isinstance(exc, VisionBudgetExhausted) and not str(exc).startswith("图片超过 7 MiB"):
                    provider_unavailable = True
                LOGGER.debug("Image vision failed: %s", exc)
                continue
            descriptions.setdefault(index, {})[ordinal] = description

    for index, by_ordinal in descriptions.items():
        values = [by_ordinal[key] for key in sorted(by_ordinal)]
        full = " ".join(value for value in values if value)
        inline = normalize_description(full, settings.max_inline_chars)
        if inline:
            messages[index]["image_vision"] = values
            messages[index]["image_vision_inline"] = inline
    if failures:
        if provider_unavailable:
            LOGGER.warning("[WARN] 图片视觉 provider 不可用，%s 张图片已降级到本地 OCR 或图片占位。", failures)
        else:
            LOGGER.warning("[WARN] %s 张图片视觉描述失败，已按单图降级到本地 OCR 或图片占位。", failures)
    return list(messages)


def annotate_moment_vision(
    posts: Sequence[Message],
    media_root: Path,
    settings: ImageVisionConfig,
    cache_root: Path,
    *,
    describer: VisionDescriber | None = None,
) -> list[Message]:
    if not settings.enabled:
        return list(posts)
    worker = describer or CheapApiVisionDescriber(settings, cache_root)
    failures = 0
    for post in posts:
        if session_is_skipped(post, [], settings.skip_usernames):
            continue
        context = build_moment_context(post, settings)
        for media in post.get("media") or []:
            if not isinstance(media, dict):
                continue
            local_path = str(media.get("localPath") or "").strip()
            image_path = media_root / local_path
            if not local_path or not image_path.is_file() or image_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
                continue
            try:
                media["image_vision_inline"] = worker.describe(image_path, context)
            except Exception as exc:
                failures += 1
                LOGGER.debug("Moment image vision failed: %s", exc)
    if failures:
        LOGGER.warning("[WARN] %s 张朋友圈图片视觉描述失败，已保留原媒体路径。", failures)
    return list(posts)


def build_chat_context(messages: Sequence[Message], image_index: int, settings: ImageVisionConfig) -> str:
    count = settings.context_messages
    if count <= 0:
        return ""
    image_day = _message_day(messages[image_index])
    start = max(0, image_index - count)
    stop = min(len(messages), image_index + count + 1)
    indices = [
        index
        for index in range(start, stop)
        if index != image_index and _message_day(messages[index]) == image_day
    ]
    image_sender = _sender_key(messages[image_index])
    others: dict[str, int] = {}
    lines: list[str] = []
    for index in indices:
        message = messages[index]
        body = _context_body(message)
        if not body:
            continue
        sender = _sender_key(message)
        if settings.anonymize_speakers:
            if sender == image_sender:
                label = "[发图人]"
            else:
                others.setdefault(sender, len(others) + 1)
                label = f"[他人{others[sender]}]"
        else:
            label = _sender_display(message)
        lines.append(f"{label}：{body}")
    return "\n".join(lines)


def build_moment_context(post: Mapping[str, Any], settings: ImageVisionConfig) -> str:
    lines: list[str] = []
    body = _single_line(str(post.get("contentDesc") or ""))
    if body:
        lines.append(f"[发图人]：{body}")
    if settings.include_moment_comments:
        others: dict[str, int] = {}
        for comment in post.get("comments") or []:
            sender = str(comment.get("username") or comment.get("nickname") or "unknown")
            others.setdefault(sender, len(others) + 1)
            comment_body = _single_line(str(comment.get("content") or ""))
            if comment_body:
                lines.append(f"[他人{others[sender]}]：{comment_body}")
    return "\n".join(lines)


def normalize_description(value: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", value.replace("｜", " ")).strip()
    if not text:
        return ""
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


def session_is_skipped(session: Mapping[str, Any], messages: Sequence[Message], skip_usernames: Sequence[str]) -> bool:
    skipped = {str(value).strip() for value in skip_usernames if str(value).strip()}
    if not skipped:
        return False
    candidates = {
        str(session.get(field) or "").strip()
        for field in ("username", "wxid", "displayName", "nickname", "remark")
    }
    candidates.update(_sender_key(message) for message in messages)
    return bool(candidates & skipped)


def _parse_response(body: Mapping[str, Any]) -> VisionResponse:
    choices = body.get("choices") or []
    if choices:
        choice = choices[0]
        content = str((choice.get("message") or {}).get("content") or "")
        return VisionResponse(content=content, finish_reason=str(choice.get("finish_reason") or ""))
    output_text = body.get("output_text")
    chunks: list[str] = [str(output_text)] if isinstance(output_text, str) else []
    finish_reason = ""
    for item in body.get("output") or []:
        finish_reason = str(item.get("finish_reason") or item.get("status") or finish_reason)
        for content in item.get("content") or []:
            if isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return VisionResponse(content="".join(chunks), finish_reason=finish_reason)


def _response_failed(response: VisionResponse) -> bool:
    return response.finish_reason == "length" or not response.content.strip()


# jisuan 家 qwen 系均为 reasoning+content 总预算语义，不得硬编码具体版本号。
def _uses_total_token_budget(settings: ImageVisionConfig) -> bool:
    return settings.provider.casefold() == "jisuan" and "qwen" in settings.model.casefold()


def _locate_api_script(runner: Runner) -> Path:
    locator = Path.home() / ".cc-switch" / "skills" / "cheap-api" / "scripts" / "locate_api_home.py"
    result = runner([sys.executable, str(locator), "--workspace", str(Path.cwd())], "", 30)
    if result.returncode != 0:
        raise VisionError(_decode_output(result.stderr).strip() or "无法定位 cheap-api")
    root = Path(_decode_output(result.stdout).strip())
    script = root / "scripts" / "cheap_api.py"
    if not script.is_file():
        raise VisionError("cheap-api 统一入口不存在")
    return script


def _run_subprocess(command: Sequence[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(command),
        input=stdin.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    for encoding in ("utf-8", "gb18030"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            pass
    return value.decode("utf-8", errors="replace")


def _context_body(message: Mapping[str, Any]) -> str:
    content = str(message.get("content") or "")
    source = str(message.get("source") or "")
    if _looks_like_image(content) or _looks_like_image(source) or "图片" in str(message.get("type") or ""):
        return "[图片]"
    return _single_line(content)


def _looks_like_image(value: str) -> bool:
    return "media/images/" in value.replace("\\", "/")


def _sender_key(message: Mapping[str, Any]) -> str:
    return str(message.get("senderUsername") or ("self" if int(message.get("isSend") or 0) == 1 else "unknown"))


def _sender_display(message: Mapping[str, Any]) -> str:
    return str(message.get("senderDisplayName") or message.get("senderUsername") or "未知")


def _message_day(message: Mapping[str, Any]) -> str:
    formatted = str(message.get("formattedTime") or "")
    if len(formatted) >= 10:
        return formatted[:10]
    timestamp = int(message.get("createTime") or 0)
    return datetime.fromtimestamp(timestamp).date().isoformat() if timestamp else ""


def _single_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
