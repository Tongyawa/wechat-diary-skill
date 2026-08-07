"""Lazy JSON-Lines client for a reusable out-of-process SenseVoice worker."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any, TextIO


class ASRUnavailableError(RuntimeError):
    """The configured optional ASR worker cannot serve further requests."""


def default_worker_script() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "sensevoice_worker.py"


class SenseVoiceTranscriber:
    """Start one worker on the first voice and reuse it for the export run."""

    def __init__(
        self,
        *,
        worker_python: str | Path,
        worker_script: str | Path | None = None,
        model: str = "iic/SenseVoiceSmall",
        language: str = "zh",
        device: str = "cpu",
        startup_timeout_sec: float = 180.0,
        request_timeout_sec: float = 120.0,
    ) -> None:
        self.worker_python = Path(worker_python)
        self.worker_script = Path(worker_script) if worker_script else default_worker_script()
        self.model_name = model
        self.language = language
        self.device = device
        self.startup_timeout_sec = float(startup_timeout_sec)
        self.request_timeout_sec = float(request_timeout_sec)
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[object] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._request_count = 0
        self._broken_reason: str | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._eof = object()

    @property
    def started(self) -> bool:
        return self._process is not None

    def transcribe(self, audio_path: str | Path) -> dict[str, Any]:
        path = Path(audio_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"语音文件不存在：{path}")

        with self._lock:
            self._ensure_worker()
            assert self._process is not None and self._process.stdin is not None
            request = json.dumps({"audio": str(path)}, ensure_ascii=False)
            first_request = self._request_count == 0
            try:
                self._process.stdin.write(request + "\n")
                self._process.stdin.flush()
                self._request_count += 1
            except (BrokenPipeError, OSError) as exc:
                self._break_worker(f"worker 写入失败：{exc}")

            timeout = self.startup_timeout_sec if first_request else self.request_timeout_sec
            try:
                response_line = self._responses.get(timeout=timeout)
            except queue.Empty:
                self._break_worker(f"worker 响应超时（{timeout:g} 秒）")
            if response_line is self._eof:
                detail = "；".join(self._stderr_tail) or "无 stderr"
                self._break_worker(f"worker 已退出：{detail}")

            try:
                payload = json.loads(str(response_line))
            except json.JSONDecodeError:
                self._break_worker("worker 返回的不是有效 JSON Lines")
            if not isinstance(payload, dict):
                self._break_worker("worker 响应顶层不是 object")
            if payload.get("error"):
                # A request-level error does not poison the resident worker.
                raise RuntimeError(str(payload["error"]))
            text = str(payload.get("text") or "").strip()
            if not text:
                raise RuntimeError("SenseVoice worker 未返回可用文字")
            emotion = payload.get("emotion")
            events = payload.get("events")
            if not isinstance(emotion, list) or not isinstance(events, list):
                self._break_worker("worker 响应缺少 emotion[]/events[]")
            return {
                "text": text,
                "emotion": [str(value) for value in emotion],
                "events": [str(value) for value in events],
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop_process()

    def _ensure_worker(self) -> None:
        if self._closed:
            raise ASRUnavailableError("SenseVoice worker 已关闭")
        if self._broken_reason:
            raise ASRUnavailableError(self._broken_reason)
        if self._process is not None:
            if self._process.poll() is None:
                return
            self._break_worker("SenseVoice worker 已意外退出")
        if not self.worker_python.is_file():
            raise ASRUnavailableError(f"worker_python 不存在：{self.worker_python}")
        if not self.worker_script.is_file():
            raise ASRUnavailableError(f"worker_script 不存在：{self.worker_script}")

        command = [
            str(self.worker_python),
            str(self.worker_script),
            "--model",
            self.model_name,
            "--language",
            self.language,
            "--device",
            self.device,
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            self._broken_reason = f"SenseVoice worker 启动失败：{exc}"
            raise ASRUnavailableError(self._broken_reason) from exc

        assert self._process.stdout is not None and self._process.stderr is not None
        threading.Thread(target=self._read_stdout, args=(self._process.stdout,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self._process.stderr,), daemon=True).start()

    def _read_stdout(self, stream: TextIO) -> None:
        try:
            for line in stream:
                text = line.strip()
                if not text:
                    continue
                try:
                    json.loads(text)
                except json.JSONDecodeError:
                    self._stderr_tail.append(f"stdout 非 JSON：{text}")
                    continue
                self._responses.put(text)
        finally:
            self._responses.put(self._eof)

    def _read_stderr(self, stream: TextIO) -> None:
        for line in stream:
            text = line.strip()
            if text:
                self._stderr_tail.append(text)

    def _break_worker(self, reason: str) -> None:
        self._broken_reason = f"SenseVoice worker 未就绪：{reason}"
        self._stop_process()
        raise ASRUnavailableError(self._broken_reason)

    def _stop_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def __enter__(self) -> "SenseVoiceTranscriber":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = ["ASRUnavailableError", "SenseVoiceTranscriber", "default_worker_script"]
