"""Offline backend for processing canonical raw files supplied by the user."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import ClassVar

from ..config import Config


@dataclass
class ManualBackend:
    """No-op port implementation; the runner consumes existing live raw."""

    name: ClassVar[str] = "manual"
    capabilities: ClassVar[frozenset[str]] = frozenset()
    config: Config

    def prepare(self) -> None:
        print(f"manual backend: place canonical raw exports in {self.config.paths.raw}")

    def export_chats(self, export_date: date) -> None:
        return None

    def export_moments(self, usernames: list[str], export_date: date) -> None:
        return None

    def transcribe_voices(self, usernames: list[str]) -> None:
        return None

    def shutdown(self) -> None:
        return None


__all__ = ["ManualBackend"]
