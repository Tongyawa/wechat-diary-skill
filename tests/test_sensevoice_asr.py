from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from wechat_diary_core.asr import ASRUnavailableError, SenseVoiceTranscriber


class _Model:
    def __init__(self):
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        return [{"text": "<|zh|><|HAPPY|><|Speech|><|withitn|>你好，世界。"}]


class SenseVoiceTests(unittest.TestCase):
    def test_model_loads_once_and_returns_text_emotion_and_events(self) -> None:
        model = _Model()
        loads = []

        def factory(**kwargs):
            loads.append(kwargs)
            return model

        transcriber = SenseVoiceTranscriber(
            model_factory=factory,
            postprocess=lambda raw: re.sub(r"<\|[^|]+\|>", "", raw),
        )
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "voice.wav"
            audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
            first = transcriber.transcribe(audio)
            second = transcriber.transcribe(audio)

        self.assertEqual(len(loads), 1)
        self.assertEqual(model.calls, 2)
        self.assertEqual(first["text"], "你好，世界。")
        self.assertEqual(first["emotion"], ["HAPPY"])
        self.assertEqual(first["events"], ["Speech"])
        self.assertEqual(second, first)

    def test_missing_optional_dependency_is_cached_as_graceful_unavailable(self) -> None:
        loads = []

        def missing(**kwargs):
            loads.append(kwargs)
            raise ModuleNotFoundError("funasr")

        transcriber = SenseVoiceTranscriber(model_factory=missing, postprocess=lambda raw: raw)
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "voice.wav"
            audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
            with self.assertRaises(ASRUnavailableError):
                transcriber.transcribe(audio)
            with self.assertRaises(ASRUnavailableError):
                transcriber.transcribe(audio)

        self.assertEqual(len(loads), 1)


if __name__ == "__main__":
    unittest.main()
