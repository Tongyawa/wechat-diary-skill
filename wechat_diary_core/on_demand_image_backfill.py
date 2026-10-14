"""Optional script bridge; image recovery and integrity stay in the supplied tool."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any


REASONS = ("file_missing", "unsupported_format", "stem_unresolved", "image_incomplete", "ffmpeg_unavailable")


@dataclass(frozen=True)
class ImageBackfillResult:
    degraded_images: int = 0
    patched_messages: int = 0
    file_missing: int = 0
    unsupported_format: int = 0
    stem_unresolved: int = 0
    image_incomplete: int = 0
    ffmpeg_unavailable: int = 0
    unchecked: int = 0
    status: str = "not_needed"

    @property
    def remaining(self) -> int:
        return self.degraded_images - self.patched_messages


class _WindowsJob:
    """Contain the gated launcher and all descendants, including orphan workers."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("user", ctypes.c_longlong), ("kernel", ctypes.c_longlong),
                ("period_user", ctypes.c_longlong), ("period_kernel", ctypes.c_longlong),
                ("faults", wintypes.DWORD), ("total", wintypes.DWORD),
                ("active", wintypes.DWORD), ("terminated", wintypes.DWORD),
            ]

        self.ctypes = ctypes
        self.accounting = Accounting
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            getattr(self.api, name).argtypes = args
            getattr(self.api, name).restype = result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.api.CloseHandle(self.handle)
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def finish(self) -> None:
        # Wait for active processes to reach zero BEFORE rollback / temp cleanup.
        try:
            if not self.api.TerminateJobObject(self.handle, 1):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            deadline = time.monotonic() + 10
            while True:
                info = self.accounting()
                if not self.api.QueryInformationJobObject(
                    self.handle, 1, self.ctypes.byref(info), self.ctypes.sizeof(info), None
                ):
                    raise self.ctypes.WinError(self.ctypes.get_last_error())
                if info.active == 0:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("补数子进程仍未结束，不能恢复 staging。")
                time.sleep(0.01)
        finally:
            self.api.CloseHandle(self.handle)


def _run_script(command: list[str], *, timeout: float) -> int:
    # The launcher waits for a byte, so the tool cannot spawn before containment.
    launcher = (
        "import runpy,sys; gate=sys.stdin.buffer.read(1); "
        "sys.argv=sys.argv[1:]; sys.path.insert(0,__import__('os').path.dirname(sys.argv[0])); "
        "runpy.run_path(sys.argv[0],run_name='__main__') if gate==b'1' else None"
    )
    job = _WindowsJob() if os.name == "nt" else None
    process = None
    try:
        process = subprocess.Popen(
            [command[0], "-c", launcher, *command[1:]],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            shell=False, start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if job is not None:
            job.assign(process)
        process.communicate(input=b"1", timeout=timeout)
        return process.returncode
    finally:
        try:
            if job is not None:
                try:
                    job.finish()
                except (OSError, RuntimeError) as exc:
                    raise RuntimeError("无法确认补数子进程已结束，拒绝继续发布。") from exc
            elif process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdin is not None:
                    process.stdin.close()


def _restore(originals: dict[Path, bytes]) -> None:
    for path, original in originals.items():
        if path.exists() and path.read_bytes() == original:
            continue
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".restore-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(original)
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _checked_report(report: Any, total: int, restored: int) -> dict[str, int]:
    if not isinstance(report, dict) or (
        type(report.get("schema_version")) is not int or report.get("schema_version") != 1
        or report.get("status") != "completed"
        or report.get("mode") != "single" or report.get("applied") is not True
    ):
        raise ValueError("invalid report envelope")
    counts = {key: report.get(key) for key in ("degraded_images", "patched_messages", *REASONS)}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("invalid report counts")
    if (
        counts["degraded_images"] != total or counts["patched_messages"] != restored
        or counts["patched_messages"] + sum(counts[key] for key in REASONS) != total
    ):
        raise ValueError("report does not match canonical output")
    return counts


def backfill_on_demand(
    session_dir: Path, *, script: Path | None, config_path: Path, require_media: bool,
) -> ImageBackfillResult:
    if not require_media:
        return ImageBackfillResult(status="media_disabled")
    originals: dict[Path, bytes] = {}
    before: dict[Path, dict[str, Any]] = {}
    candidates: list[tuple[Path, int]] = []
    for path in sorted(session_dir.glob("*.json")):
        original = path.read_bytes()
        payload = json.loads(original.decode("utf-8"))
        originals[path] = original
        if (payload.get("weflow") or {}).get("source") != "http_api":
            continue
        before[path] = payload
        for index, message in enumerate(payload.get("messages") or []):
            if message.get("type") == "图片消息" and message.get("source") == "":
                candidates.append((path, index))
    total = len(candidates)
    if total == 0:
        return ImageBackfillResult()
    if script is None:
        return ImageBackfillResult(degraded_images=total, unchecked=total, status="not_configured")
    if not script.is_file():
        return ImageBackfillResult(degraded_images=total, unchecked=total, status="unavailable")
    print(f"图片补数：正在检查 {total} 条降级图片。", file=sys.stderr)
    status = "failed"
    media_root = session_dir.parent / "_media"
    original_media = {path for path in media_root.rglob("*") if path.is_file()}
    # Six canonical filename variants per image, up to 60s per wxgf invocation,
    # plus the 180s worker and 60s setup margin. Exceeding this is a soft failure.
    timeout = 240 + 360 * total
    try:
        with tempfile.TemporaryDirectory(prefix="on-demand-image-report-") as temporary:
            report_path = Path(temporary) / "report.json"
            exit_code = _run_script(
                [sys.executable, str(script), "--canonical-dir", str(session_dir),
                 "--config", str(config_path), "--apply", "--report-json", str(report_path)],
                timeout=timeout,
            )
            if exit_code != 0:
                raise ValueError("backfill did not complete")
            if set(session_dir.glob("*.json")) != set(originals):
                raise ValueError("canonical file set changed")
            after = {path: json.loads(path.read_text(encoding="utf-8")) for path in originals}
            restored = 0
            for path, old in before.items():
                new = after[path]
                if len(new.get("messages") or []) != len(old.get("messages") or []):
                    raise ValueError("message count changed")
                for old_message, new_message in zip(old["messages"], new["messages"]):
                    if old_message.get("source") and new_message.get("source") != old_message["source"]:
                        raise ValueError("existing source changed")
            store = (session_dir.parent / "_media" / "images").resolve()
            for path, index in candidates:
                source = after[path]["messages"][index].get("source")
                if not source:
                    continue
                if not isinstance(source, str) or not source.startswith("../_media/images/"):
                    raise ValueError("invalid recovered source")
                media = (session_dir / source).resolve()
                media.relative_to(store)
                with media.open("rb") as handle:
                    if not handle.read(1):
                        raise ValueError("empty recovered media")
                restored += 1
            counts = _checked_report(json.loads(report_path.read_text(encoding="utf-8")), total, restored)
            return ImageBackfillResult(**counts, status="completed")
    except subprocess.TimeoutExpired:
        status = "timed_out"
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        pass
    except KeyboardInterrupt:
        _restore(originals)
        raise
    _restore(originals)  # Failure here is an output-filesystem error, never exit 0.
    # Discard this failed attempt's additions; preserve existing API media.
    for path in session_dir.glob("*.json"):
        if path not in originals:
            path.resolve().relative_to(session_dir.resolve())
            path.unlink()
    for path in media_root.rglob("*"):
        if path.is_file() and path not in original_media:
            path.resolve().relative_to(media_root.resolve())
            path.unlink()
    return ImageBackfillResult(degraded_images=total, unchecked=total, status=status)


def powershell_command(arguments: list[str]) -> str:
    return "& " + " ".join("'" + value.replace("'", "''") + "'" for value in arguments)


def print_backfill_result(
    result: ImageBackfillResult, *, script: Path | None, config_path: Path,
    raw_session_dir: Path, process_script: Path,
) -> None:
    if result.degraded_images == 0:
        return
    print(
        f"图片补数：补回 {result.patched_messages}；按现役规则未找到候选 {result.file_missing}；"
        f"格式不支持 {result.unsupported_format}；无法关联 {result.stem_unresolved}；"
        f"结构或解码失败 {result.image_incomplete}；ffmpeg 不可用 {result.ffmpeg_unavailable}；"
        f"未检查 {result.unchecked}。仍为 [图片]：{result.remaining}。",
        file=sys.stderr,
    )
    if result.remaining == 0:
        return
    print(
        "[WARN] 图片保留为 [图片]：这些多半是 API 不支持的老格式，字节可能仍在本地。"
        "补数" + ("已检查，仍有未补回项" if result.status == "completed" else "未执行或未完成")
        + "；导出已继续。", file=sys.stderr,
    )
    if script is None:
        print("可在 config.toml 配置 [image_backfill].script 指向本机补数脚本后重试。", file=sys.stderr)
        return
    if result.status == "unavailable":
        print(f"配置的补数脚本不存在：{script}", file=sys.stderr)
        print("请修正 config.toml 的 [image_backfill].script，指向有效的本机补数脚本后重试。", file=sys.stderr)
        return
    print("手工补数（先确认脚本及微信目录可读）：", file=sys.stderr)
    print(powershell_command([
        sys.executable, str(script), "--canonical-dir", str(raw_session_dir),
        "--config", str(config_path), "--apply",
    ]), file=sys.stderr)
    print(
        f"补 raw 后 processed / merged 尚未更新。离线重建入口：{process_script}；"
        "先准备隔离 config，将 processed / archived 指向本次独立输出，"
        "不要使用日常数据落点；合并文件还需另行重建。", file=sys.stderr,
    )
