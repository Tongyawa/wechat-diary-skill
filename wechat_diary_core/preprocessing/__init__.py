from __future__ import annotations

from pathlib import Path

from ..config import Config, load_config
from .cleaner import ProcessedChatExport, discover_chat_exports, preprocess_export
from .exceptions import InvalidExportError, PreprocessingError
from .image_ocr import OcrEngine


def run(
    raw_path: str | Path,
    config: Config | None = None,
    ocr_engine: OcrEngine | None = None,
) -> list[ProcessedChatExport]:
    cfg = config or load_config()
    exports: list[ProcessedChatExport] = []
    for source_path in discover_chat_exports(raw_path):
        processed = preprocess_export(source_path, config=cfg, ocr_engine=ocr_engine)
        if processed.data.get("messages"):
            exports.append(processed)
    return exports


__all__ = [
    "InvalidExportError",
    "OcrEngine",
    "PreprocessingError",
    "ProcessedChatExport",
    "discover_chat_exports",
    "preprocess_export",
    "run",
]
