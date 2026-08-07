from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts import sensevoice_worker
from wechat_diary_core.asr import ASRUnavailableError, SenseVoiceTranscriber


FAKE_WORKER = r'''
import json
import os
import sys
from pathlib import Path

counter = Path(__file__).with_suffix(".count")
starts = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
counter.write_text(str(starts + 1), encoding="utf-8")
for line in sys.stdin:
    request = json.loads(line)
    name = Path(request["audio"]).name
    if "crash" in name:
        os._exit(7)
    if "fail" in name:
        payload = {"error": "fixture request failure"}
    else:
        payload = {"text": "你好，世界。", "emotion": ["HAPPY"], "events": ["Speech"]}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
'''

NOISY_WORKER = r'''
import json
import os
import sys

for line in sys.stdin:
    request = json.loads(line)
    sys.stdout.write("funasr version: 1.4.1.\n")
    sys.stdout.flush()
    if os.environ.get("FAKE_WORKER_EXIT_AFTER_NOISE"):
        os._exit(7)
    payload = {"text": "噪声后的结果", "emotion": ["HAPPY"], "events": ["Speech"]}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
'''


def _audio(root: Path, name: str) -> Path:
    path = root / name
    path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    return path


class _Model:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        return [{"text": "<|zh|><|HAPPY|><|Speech|><|withitn|>你好，世界。"}]


class SenseVoiceTests(unittest.TestCase):
    def test_jsonl_client_starts_lazily_and_reuses_one_worker_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "fake_worker.py"
            worker.write_text(FAKE_WORKER, encoding="utf-8")
            transcriber = SenseVoiceTranscriber(
                worker_python=sys.executable,
                worker_script=worker,
                startup_timeout_sec=10,
                request_timeout_sec=10,
            )
            self.assertFalse(transcriber.started)
            first = transcriber.transcribe(_audio(root, "first.wav"))
            second = transcriber.transcribe(_audio(root, "second.wav"))
            transcriber.close()

            self.assertEqual((root / "fake_worker.count").read_text(encoding="utf-8"), "1")
            self.assertEqual(first["text"], "你好，世界。")
            self.assertEqual(first["emotion"], ["HAPPY"])
            self.assertEqual(first["events"], ["Speech"])
            self.assertEqual(second, first)

    def test_jsonl_client_skips_non_json_stdout_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "noisy_worker.py"
            worker.write_text(NOISY_WORKER, encoding="utf-8")
            transcriber = SenseVoiceTranscriber(
                worker_python=sys.executable,
                worker_script=worker,
                startup_timeout_sec=10,
                request_timeout_sec=10,
            )
            try:
                result = transcriber.transcribe(_audio(root, "voice.wav"))
            finally:
                transcriber.close()

            self.assertEqual(result["text"], "噪声后的结果")
            self.assertEqual(result["emotion"], ["HAPPY"])
            self.assertEqual(result["events"], ["Speech"])

    def test_jsonl_client_noise_then_eof_is_still_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "noisy_worker.py"
            worker.write_text(NOISY_WORKER, encoding="utf-8")
            transcriber = SenseVoiceTranscriber(
                worker_python=sys.executable,
                worker_script=worker,
                startup_timeout_sec=10,
                request_timeout_sec=10,
            )
            with mock.patch.dict(os.environ, {"FAKE_WORKER_EXIT_AFTER_NOISE": "1"}):
                with self.assertRaisesRegex(ASRUnavailableError, "worker 已退出"):
                    transcriber.transcribe(_audio(root, "voice.wav"))
            transcriber.close()

    def test_unconfigured_worker_is_optional_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcriber = SenseVoiceTranscriber(worker_python=root / "missing-python")
            with self.assertRaisesRegex(ASRUnavailableError, "worker_python"):
                transcriber.transcribe(_audio(root, "voice.wav"))
            self.assertFalse(transcriber.started)

    def test_request_error_does_not_kill_worker_but_crash_disables_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "fake_worker.py"
            worker.write_text(FAKE_WORKER, encoding="utf-8")
            transcriber = SenseVoiceTranscriber(
                worker_python=sys.executable,
                worker_script=worker,
                startup_timeout_sec=10,
                request_timeout_sec=10,
            )
            with self.assertRaisesRegex(RuntimeError, "fixture request failure"):
                transcriber.transcribe(_audio(root, "fail.wav"))
            self.assertEqual(transcriber.transcribe(_audio(root, "after-error.wav"))["text"], "你好，世界。")
            with self.assertRaises(ASRUnavailableError):
                transcriber.transcribe(_audio(root, "crash.wav"))
            with self.assertRaises(ASRUnavailableError):
                transcriber.transcribe(_audio(root, "after-crash.wav"))
            transcriber.close()

            self.assertEqual((root / "fake_worker.count").read_text(encoding="utf-8"), "1")

    def test_worker_loads_model_once_and_encodes_each_protocol_response(self) -> None:
        model = _Model()
        loads = []

        def factory(**kwargs):
            loads.append(kwargs)
            return model

        input_stream = io.StringIO(
            json.dumps({"audio": "first.wav"}) + "\n" + json.dumps({"audio": "second.wav"}) + "\n"
        )
        output_stream = io.StringIO()
        exit_code = sensevoice_worker.serve(
            input_stream,
            output_stream,
            model_name="iic/SenseVoiceSmall",
            language="zh",
            device="cpu",
            model_factory=factory,
            postprocess=lambda raw: re.sub(r"<\|[^|]+\|>", "", raw),
        )
        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(loads), 1)
        self.assertEqual(model.calls, 2)
        self.assertEqual(responses[0]["text"], "你好，世界。")
        self.assertEqual(responses[0]["emotion"], ["HAPPY"])
        self.assertEqual(responses[0]["events"], ["Speech"])
        self.assertEqual(responses[1], responses[0])

    def test_worker_redirects_third_party_stdout_away_from_protocol(self) -> None:
        class NoisyModel:
            def generate(self, **_kwargs):
                print("generate noise")
                return [{"text": "<|zh|><|HAPPY|><|Speech|><|withitn|>你好。"}]

        def noisy_factory(**_kwargs):
            print("import noise")
            return NoisyModel()

        funasr = types.ModuleType("funasr")
        funasr.__path__ = []
        funasr.AutoModel = noisy_factory
        utils = types.ModuleType("funasr.utils")
        utils.__path__ = []
        postprocess_module = types.ModuleType("funasr.utils.postprocess_utils")
        postprocess_module.rich_transcription_postprocess = lambda raw: re.sub(r"<\|[^|]+\|>", "", raw)
        protocol_output = io.StringIO()
        diagnostics = io.StringIO()
        input_stream = io.StringIO(json.dumps({"audio": "voice.wav"}) + "\n")
        modules = {
            "funasr": funasr,
            "funasr.utils": utils,
            "funasr.utils.postprocess_utils": postprocess_module,
        }

        with mock.patch.dict(sys.modules, modules):
            with mock.patch.object(sys, "stdin", input_stream):
                with mock.patch.object(sys, "stdout", protocol_output):
                    with mock.patch.object(sys, "stderr", diagnostics):
                        exit_code = sensevoice_worker.main([])

        protocol_lines = protocol_output.getvalue().splitlines()
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(protocol_lines), 1)
        self.assertEqual(json.loads(protocol_lines[0])["text"], "你好。")
        self.assertIn("import noise", diagnostics.getvalue())
        self.assertIn("generate noise", diagnostics.getvalue())


if __name__ == "__main__":
    unittest.main()
