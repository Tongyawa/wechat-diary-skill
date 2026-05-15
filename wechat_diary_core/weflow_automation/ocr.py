from __future__ import annotations

from pathlib import Path

from ..preprocessing.image_ocr import RapidOcrEngine


def read_text(image_path: str | Path, min_confidence: float = 0.55) -> list[str]:
    return RapidOcrEngine().read_text(Path(image_path), min_confidence)
