"""One-attempt, credential-isolating subprocess transports. No cache or retries."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Callable, Protocol, Sequence

from ..config import ImageVisionConfig
from .vision_records import ProviderUnavailable, VisionBudgetExhausted, VisionError


MAX_IMAGE_BYTES = 7 * 1024 * 1024
Runner = Callable[[Sequence[str], str, float], subprocess.CompletedProcess]


@dataclass(frozen=True)
class VisionResponse:
    content: str
    finish_reason: str = ""


class VisionTransport(Protocol):
    def request(self, paths: Sequence[Path], context: str, system: str, schema: dict | None,
                *, max_tokens: int | None = None) -> VisionResponse: ...


def decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    for encoding in ("utf-8", "gb18030"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace")


def run_subprocess(command: Sequence[str], stdin: str, timeout: float) -> subprocess.CompletedProcess:
    job = None
    if os.name == "nt":
        from .vision_job import WindowsJob
        job = WindowsJob()
    # CREATE_SUSPENDED prevents a fast child from spawning outside the job before
    # it has been assigned. Only this call's process handle is resumed or killed.
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | 0x4} if os.name == "nt" else {"start_new_session": True}
    try:
        with subprocess.Popen(list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options) as proc:
            try:
                if job:
                    job.assign(proc._handle)
                    job.resume(proc._handle)
                stdout, stderr = proc.communicate(stdin.encode("utf-8"), timeout=timeout)
            except BaseException:
                if job:
                    try:
                        job.terminate()
                    finally:
                        job.close()
                else:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=5)
                raise
            return subprocess.CompletedProcess(list(command), proc.returncode, stdout, stderr)
    finally:
        if job:
            job.close()


def response_failed(response: VisionResponse) -> bool:
    return response.finish_reason == "length" or not response.content.strip()


def parse_response(body: dict) -> VisionResponse:
    choices = body.get("choices") or []
    if choices:
        choice = choices[0]
        return VisionResponse(str((choice.get("message") or {}).get("content") or ""), str(choice.get("finish_reason") or ""))
    chunks = [body["output_text"]] if isinstance(body.get("output_text"), str) else []
    reason = ""
    for item in body.get("output") or []:
        reason = str(item.get("finish_reason") or item.get("status") or reason)
        chunks.extend(x["text"] for x in item.get("content", []) if isinstance(x.get("text"), str))
    return VisionResponse("".join(chunks), reason)


def locate_api_script(runner: Runner) -> Path:
    locator = Path.home() / ".cc-switch/skills/cheap-api/scripts/locate_api_home.py"
    result = runner([sys.executable, str(locator), "--workspace", str(Path.cwd())], "", 30)
    if result.returncode:
        raise ProviderUnavailable("cheap-api locator failed")
    script = Path(decode_output(result.stdout).strip()) / "scripts/cheap_api.py"
    if not script.is_file():
        raise ProviderUnavailable("cheap-api entrypoint missing")
    return script


class CheapApiTransport:
    def __init__(self, settings: ImageVisionConfig, *, runner: Runner | None = None, api_script: Path | None = None):
        self.settings = settings
        self.runner = runner or run_subprocess
        self.api_script = api_script

    def model_identity(self):
        script = self.api_script or locate_api_script(self.runner)
        self.api_script = script
        catalog = script.parent.parent / "config" / "cheap_api.json"
        try:
            body = json.loads(catalog.read_text(encoding="utf-8"))
            models = body["providers"][self.settings.provider]["models"]
            value = models.get(self.settings.model, self.settings.model)
            model = value if isinstance(value, str) else value.get("id", value.get("model", self.settings.model))
            return str(model)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # Never confuse a provider-local alias with a concrete model ID.
            raise ProviderUnavailable("cheap-api model catalog unavailable") from exc

    def request(self, paths, context, system, schema=None, *, max_tokens=None):
        if any(path.stat().st_size > MAX_IMAGE_BYTES for path in paths):
            raise VisionError("image_over_7_mib")
        script = self.api_script or locate_api_script(self.runner)
        command = [sys.executable, str(script), "--provider", self.settings.provider,
                   "--model", self.settings.model, "--system", system]
        for path in paths:
            command.extend(["--image", str(path)])
        command.extend(["--max-tokens", str(max_tokens or self.settings.max_tokens),
                        "--timeout", str(self.settings.timeout_sec), "--json"])
        result = self.runner(command, context, self.settings.timeout_sec + 60)
        if result.returncode:
            # Do not echo stderr: providers may include a request or credential.
            raise ProviderUnavailable(f"cheap-api exit {result.returncode}")
        try:
            response = parse_response(json.loads(decode_output(result.stdout)))
        except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
            raise VisionError("cheap-api invalid envelope") from exc
        if response_failed(response):
            raise VisionBudgetExhausted("vision_empty_or_length")
        return response


def codex_executable(command: str) -> list[str]:
    if command.endswith(".js"):
        return ["node", command]
    resolved = shutil.which(command) or command
    if Path(resolved).suffix.lower() in {".cmd", ".ps1"}:
        # Avoid shell quoting/argument interpretation in npm shims.
        script = Path(resolved).parent / "node_modules/@openai/codex/bin/codex.js"
        if script.is_file():
            return ["node", str(script)]
        raise ProviderUnavailable("configure codex_command with a native executable or JS entrypoint")
    return [resolved]


class CodexTransport:
    def __init__(self, settings: ImageVisionConfig, *, runner: Runner | None = None):
        self.settings = settings
        self.runner = runner or run_subprocess

    def request(self, paths, context, system, schema=None, *, max_tokens=None):
        with tempfile.TemporaryDirectory(prefix="diary-vision-") as directory:
            root = Path(directory)
            last = root / "answer.txt"
            command = codex_executable(self.settings.codex_command) + [
                "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "-C", str(root),
                "-s", "read-only", "--color", "never", "-m", self.settings.model,
                "-c", f'model_reasoning_effort="{self.settings.effort}"',
                "-c", "features.shell_tool=false", "-c", 'web_search="disabled"',
                "--json", "-o", str(last)]
            if schema is not None:
                schema_path = root / "schema.json"
                schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
                command.extend(["--output-schema", str(schema_path)])
            for path in paths:
                command.extend(["-i", str(path.resolve())])
            command.append("-")
            try:
                result = self.runner(command, system + "\n\n" + context, self.settings.timeout_sec)
            except FileNotFoundError as exc:
                raise ProviderUnavailable("codex executable missing; configure image_vision.codex_command") from exc
            if result.returncode:
                raise ProviderUnavailable(f"codex exit {result.returncode}")
            for line in decode_output(result.stdout).splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    raise VisionError("codex_invalid_event")
                item = event.get("item")
                if item is not None and not isinstance(item, dict):
                    raise VisionError("codex_invalid_event")
                item = item or {}
                kind = item.get("type") or item.get("item_type")
                if kind and kind not in {"agent_message", "reasoning"}:
                    raise VisionError("codex_unexpected_tool_event")
                if event.get("type") in {"turn.failed", "error"}:
                    raise VisionError("codex_failed_turn")
            try:
                content = last.read_text(encoding="utf-8")
            except OSError as exc:
                raise VisionError("codex_missing_output") from exc
            if not content.strip():
                raise VisionBudgetExhausted("codex_empty_output")
            return VisionResponse(content)
