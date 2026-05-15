from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol
import logging

from ..config import PreprocessingConfig


LOGGER = logging.getLogger(__name__)
Message = dict[str, Any]
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


class OcrEngine(Protocol):
    def read_text(self, image_path: Path, min_confidence: float) -> list[str]: ...


class RapidOcrEngine:
    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            try:
                from rapidocr import RapidOCR
            except ImportError as exc:
                raise RuntimeError("RapidOCR is not installed") from exc
        self._engine = RapidOCR()

    def read_text(self, image_path: Path, min_confidence: float) -> list[str]:
        result = self._engine(str(image_path))
        entries = result[0] if isinstance(result, tuple) else result
        if not entries:
            return []

        lines: list[str] = []
        for entry in entries:
            text, score = _parse_ocr_entry(entry)
            if text and score >= min_confidence:
                lines.append(text)
        return lines


def annotate_image_messages(
    messages: Sequence[Message],
    base_dir: Path,
    settings: PreprocessingConfig,
    engine: OcrEngine | None = None,
) -> list[Message]:
    if not settings.image_ocr_enabled:
        return list(messages)

    image_messages = [message for message in messages if _message_image_paths(message, base_dir)]
    if not image_messages:
        return list(messages)

    if engine is None:
        try:
            engine = RapidOcrEngine()
        except RuntimeError:
            LOGGER.warning("RapidOCR is unavailable; image OCR was skipped.")
            return list(messages)

    for message in image_messages:
        ocr_lines: list[str] = []
        for image_path in _message_image_paths(message, base_dir):
            if image_path.exists():
                ocr_lines.extend(engine.read_text(image_path, settings.image_ocr_min_confidence))

        if ocr_lines:
            text = "\n".join(ocr_lines)
            message["image_ocr"] = ocr_lines
            content = str(message.get("content") or "").strip()
            message["content"] = f"{content}\n[OCR] {text}" if content else f"[OCR] {text}"

    return list(messages)


def _message_image_paths(message: Message, base_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for field in ("content", "source"):
        value = message.get(field)
        if isinstance(value, str) and _looks_like_image_ref(value):
            paths.append(_resolve_message_path(base_dir, value))
    return paths


def _looks_like_image_ref(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return "media/images/" in normalized and Path(normalized).suffix.lower() in IMAGE_EXTENSIONS


def _resolve_message_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


def _parse_ocr_entry(entry: Any) -> tuple[str, float]:
    if isinstance(entry, dict):
        return str(entry.get("text") or ""), float(entry.get("confidence") or entry.get("score") or 0)
    if isinstance(entry, (list, tuple)) and len(entry) >= 3:
        return str(entry[1] or ""), float(entry[2] or 0)
    return "", 0.0
