from __future__ import annotations

from pathlib import Path

from ..config import Config, load_config
from .cleaner import ProcessedChatExport, discover_chat_exports, preprocess_export
from .exceptions import InvalidExportError, PreprocessingError
from .image_ocr import ImageMode, OcrEngine
from .moments import (
    MomentsExport,
    archive_moments_for,
    discover_moments_exports,
    load_moments_export,
    render_moments_flow,
)


def run(
    raw_path: str | Path,
    config: Config | None = None,
    ocr_engine: OcrEngine | None = None,
    image_mode: ImageMode = "ocr_inline",
) -> list[ProcessedChatExport]:
    cfg = config or load_config()
    exports: list[ProcessedChatExport] = []
    for source_path in discover_chat_exports(raw_path):
        processed = preprocess_export(source_path, config=cfg, ocr_engine=ocr_engine, image_mode=image_mode)
        if processed.data.get("messages"):
            exports.append(processed)
    return exports


__all__ = [
    "ImageMode",
    "InvalidExportError",
    "MomentsExport",
    "OcrEngine",
    "PreprocessingError",
    "ProcessedChatExport",
    "archive_moments_for",
    "discover_chat_exports",
    "discover_moments_exports",
    "load_moments_export",
    "preprocess_export",
    "render_moments_flow",
    "run",
]
