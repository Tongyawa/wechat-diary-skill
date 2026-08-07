"""Optional out-of-process speech-recognition capabilities."""

from .sensevoice import ASRUnavailableError, SenseVoiceTranscriber, default_worker_script

__all__ = ["ASRUnavailableError", "SenseVoiceTranscriber", "default_worker_script"]
