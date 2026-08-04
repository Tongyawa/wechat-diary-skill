"""Export backend port and built-in backend registry."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from wechat_diary_core.config import Config


class ExporterBackend(Protocol):
    """Produce canonical raw exports for the source-independent pipeline.

    ``voice_transcribe`` means the backend exposes a separately schedulable
    transcription stage. API-based backends may instead transcribe inline in
    :meth:`export_chats` and omit that capability.
    """

    name: str
    capabilities: frozenset[str]

    def prepare(self) -> None: ...

    def export_chats(self, export_date: date) -> None: ...

    def export_moments(self, usernames: list[str], export_date: date) -> None: ...

    def transcribe_voices(self, usernames: list[str]) -> None: ...

    def shutdown(self) -> None: ...


from .weflow.backend import WeflowBackend


REGISTRY: dict[str, type[WeflowBackend]] = {"weflow": WeflowBackend}


def create_backend(name: str, config: Config) -> ExporterBackend:
    """Instantiate a registered backend by its stable config name."""

    try:
        backend_type = REGISTRY[name]
    except KeyError as exc:
        supported = ", ".join(sorted(REGISTRY))
        raise ValueError(f"Unsupported export backend: {name!r}. Available: {supported}") from exc
    return backend_type(config)


__all__ = ["ExporterBackend", "REGISTRY", "WeflowBackend", "create_backend"]
