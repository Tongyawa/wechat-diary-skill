"""Optional local speech-recognition capabilities."""

from .sensevoice import ASRUnavailableError, SenseVoiceTranscriber, sensevoice_dependencies_available

__all__ = ["ASRUnavailableError", "SenseVoiceTranscriber", "sensevoice_dependencies_available"]
