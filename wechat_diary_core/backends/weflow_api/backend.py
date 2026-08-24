"""ExporterBackend implementation for the WeFlow 5.x local HTTP API."""

from __future__ import annotations

import json
import re
import subprocess
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, ClassVar

from ...asr import SenseVoiceTranscriber
from ...config import Config
from .client import WeflowApiClient, WeflowApiError
from .failure_state import SessionFailureState
from .mapper import ImageMediaStats, write_moments_export, write_session_export


NO_LOCAL_RECORDS_STALE_DAYS = 365
_SKIPPED_ACCOUNT_SUFFIXES = ("@openim", "@opencustomerservicemsg")


@dataclass
class WeflowApiBackend:
    name: ClassVar[str] = "weflow_api"
    capabilities: ClassVar[frozenset[str]] = frozenset({"moments"})

    config: Config
    client_factory: Callable[..., WeflowApiClient] = field(default=WeflowApiClient, repr=False)
    process_launcher: Callable[[Path], Any] = field(default=None, repr=False)  # type: ignore[assignment]
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    partial_failures: list[str] = field(default_factory=list, init=False)
    _client: WeflowApiClient | None = field(default=None, init=False, repr=False)
    _transcriber: Any = field(default=None, init=False, repr=False)
    _transcriber_initialized: bool = field(default=False, init=False, repr=False)
    _asr_unavailable_reason: str = field(default="ASR未启用", init=False, repr=False)
    _failure_state: SessionFailureState | None = field(default=None, init=False, repr=False)
    _failure_state_writable: bool = field(default=True, init=False, repr=False)
    _last_export_date: date | None = field(default=None, init=False, repr=False)
    _sessions_cache: list[dict[str, Any]] | None = field(default=None, init=False, repr=False)
    _contacts_cache: list[dict[str, Any]] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.process_launcher is None:
            self.process_launcher = _launch_weflow_normally

    @property
    def client(self) -> WeflowApiClient:
        if self._client is None:
            api = self.config.export_backend.weflow_api
            self._client = self.client_factory(
                api.base_url,
                api.access_token,
                timeout=api.request_timeout_sec,
                message_timeout=api.message_request_timeout_sec,
            )
        return self._client

    def prepare(self) -> None:
        api = self.config.export_backend.weflow_api
        if not api.access_token:
            raise RuntimeError(
                "WeFlow API Access Token 未配置。请打开 WeFlow → 设置 → API 服务，"
                "生成并固定非空 token，写入 config.toml 后重启 API 服务。"
            )
        try:
            self._probe_ready()
        except Exception as first_error:
            if isinstance(first_error, WeflowApiError) and first_error.status == 401:
                raise RuntimeError(
                    "WeFlow API Access Token 不匹配。请固定非空 token 并重启 API 服务后重试。"
                ) from first_error
            executable = self.config.export_backend.weflow.weflow_exe
            if not executable.is_file():
                raise RuntimeError(
                    "WeFlow API 服务未启动。请打开 WeFlow → 设置 → API 服务 → 启动服务，"
                    "并确认 Access Token 已固定；也可配置有效的 weflow_exe 供程序普通启动。"
                ) from first_error
            try:
                self.process_launcher(executable)
            except Exception:
                pass
            deadline = time.monotonic() + self.config.export_backend.weflow.launch_timeout_sec
            last_error: BaseException = first_error
            while time.monotonic() < deadline:
                try:
                    self._probe_ready()
                except Exception as exc:
                    last_error = exc
                    self.sleep(1.0)
                else:
                    self._validate_configured_contacts_before_mutation()
                    return
            raise RuntimeError(
                "WeFlow API 服务未启动或鉴权失败。请打开 WeFlow → 设置 → API 服务 → "
                "启动服务，并确认 Access Token 已固定；修改 token 后需重启 API 服务。"
                f" 最后错误：{last_error}"
            ) from first_error
        else:
            self._validate_configured_contacts_before_mutation()
            return

    def export_chats(self, export_date: date) -> None:
        self.partial_failures.clear()
        sessions, contacts = self._export_roster()
        self._validate_configured_contacts(sessions, contacts)

        state = self._load_failure_state()
        self._last_export_date = export_date
        no_local_records_before = len(state.no_local_records)
        historical_wxids, history_scan_complete = _historical_export_wxids(
            self.config.paths.archived / "raw"
        )
        successful_requests = 0
        messages_found = 0
        published_sessions = 0
        skipped_platform_accounts = 0
        ignored_failures = 0
        for session in sessions:
            talker = str(session.get("username") or "")
            if not talker:
                self._record_session_failure("unknown", "会话缺少 username", None)
                continue
            if self.config.daily_export.skip_official_accounts and _is_platform_account(talker):
                skipped_platform_accounts += 1
                state.record_success(talker)
                continue
            display_name = str(
                session.get("displayName")
                or session.get("nickname")
                or session.get("remark")
                or talker
            )
            try:
                messages = self.client.fetch_messages(
                    talker,
                    start=export_date,
                    end=export_date,
                    media=self.config.export_backend.weflow_api.media_localize,
                )
                successful_requests += 1
                if not messages:
                    self._record_session_success(state, talker, display_name)
                    continue
                messages_found += 1
                members = self.client.fetch_group_members(talker) if talker.endswith("@chatroom") else []
                transcriber, reason = self._asr()
                image_media_stats = ImageMediaStats()
                write_session_export(
                    self.config.paths.raw,
                    session,
                    messages,
                    start=export_date,
                    end=export_date,
                    contacts=contacts,
                    group_members=members,
                    self_wxids=self.config.user.self_wxids,
                    transcriber=transcriber,
                    asr_unavailable_reason=reason,
                    emit_emotion=self.config.asr.emit_emotion,
                    require_media=self.config.export_backend.weflow_api.media_localize,
                    appmsg_text_max_chars=self.config.export_backend.weflow_api.appmsg_text_max_chars,
                    image_media_stats=image_media_stats,
                )
                if image_media_stats.missing_image_paths or image_media_stats.missing_image_files:
                    marker = (
                        f"export_chat_image_media:{talker}:"
                        f"missing_path={image_media_stats.missing_image_paths}:"
                        f"missing_file={image_media_stats.missing_image_files}"
                    )
                    self.partial_failures.append(marker)
                    readable_name = _readable_error_summary(display_name, limit=60)
                    print(
                        f"[WARN] 会话「{readable_name}」有图片降级为 [图片]："
                        f"缺少本地路径 {image_media_stats.missing_image_paths} 条；"
                        f"路径指向的文件不存在 {image_media_stats.missing_image_files} 条。已继续导出。",
                        file=sys.stderr,
                    )
                published_sessions += 1
                self._record_session_success(state, talker, display_name)
            except Exception as exc:
                is_no_local_records, last_timestamp = _is_no_local_records_failure(
                    exc,
                    session,
                    export_date,
                    historical_wxids=historical_wxids,
                    history_scan_complete=history_scan_complete,
                )
                if is_no_local_records:
                    state.record_no_local_records(
                        talker,
                        display_name,
                        export_date,
                        str(exc),
                        last_timestamp=last_timestamp,
                    )
                    continue
                update = state.record_failure(talker, display_name, export_date, str(exc))
                if update.fingerprint_changed:
                    marker = f"export_chat_session:{talker}"
                    self.partial_failures.append(marker)
                    print(
                        f"[WARN] 会话「{display_name}」出现新的失败类型（原因已变化），"
                        f"已重新纳入审查：{update.record['lastError']}",
                        file=sys.stderr,
                    )
                elif update.was_ignored:
                    ignored_failures += 1
                    self._append_ignored_failure_detail(talker, display_name, str(exc))
                else:
                    self._record_session_failure(talker, str(exc), display_name)

        self._save_failure_state()
        no_local_records_after = len(state.no_local_records)
        if no_local_records_after != no_local_records_before:
            delta = no_local_records_after - no_local_records_before
            print(
                f"[INFO] 本机无记录会话现有 {no_local_records_after} 个"
                f"（数量变化 {delta:+d}）；已移出失败审查"
            )
        if skipped_platform_accounts:
            print(
                f"[INFO] 已跳过 {skipped_platform_accounts} 个公众号/企业微信/客服会话"
                "（skip_official_accounts=true）"
            )
        if ignored_failures:
            print(f"[INFO] {ignored_failures} 个已忽略会话仍导出失败（明细见 runlog）")

        if self.partial_failures and (successful_requests == 0 or (messages_found > 0 and published_sessions == 0)):
            raise RuntimeError("全部会话请求失败，未发布任何 canonical raw")

    def review_session_failures(
        self,
        *,
        interactive: bool,
        input_func: Callable[[str], str],
    ) -> None:
        state = self._failure_state or self._load_failure_state()
        pending = state.pending_review()
        if not pending:
            return

        print(f"[INFO] {len(pending)} 个会话连续失败达到待审查阈值：")
        for record in pending:
            reason = _readable_error_summary(str(record["lastError"]))
            print(
                f"[INFO]   - 「{record['displayName']}」({record['wxid']})："
                f"连续 {record['consecutiveFailures']} 个导出日；{reason}"
            )
        if not interactive:
            print("[INFO] 当前为非交互运行；下次交互式运行时可确认是否忽略后续失败提示。")
            return

        print("[INFO] 忽略后仍会每天尝试导出，恢复成功时会自动移出忽略名单。", flush=True)
        print("[INFO] 请选择：[a] 全部忽略 / [k] 全部保留（默认）", flush=True)
        choice = input_func("").strip().lower()
        if choice not in {"a", "all", "y", "yes", "全部", "全部忽略"}:
            print("[INFO] 已保留全部待审查会话；后续失败继续按 WARN 提示。")
            return
        authorized_date = self._last_export_date or date.today()
        changed = state.ignore((str(record["wxid"]) for record in pending), authorized_date=authorized_date)
        self._save_failure_state()
        print(f"[INFO] 已忽略 {changed} 个会话的后续失败提示；仍会每日尝试以便自动恢复。")

    def export_moments(self, usernames: list[str], export_date: date) -> None:
        raw_root = self.config.paths.raw
        staging_parent = raw_root.parent / f".{raw_root.name}.weflow-api-moments-staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(tempfile.mkdtemp(prefix=f"moments-{export_date:%Y%m%d}-", dir=staging_parent))
        try:
            response = self.client.export_moments(
                staging_dir,
                usernames,
                start=export_date,
                end=export_date,
            )
            file_path = str(response.get("filePath") or "")
            if response.get("postCount") == 0 and not file_path:
                return
            if not file_path:
                raise WeflowApiError("WeFlow 朋友圈导出未返回 filePath")
            source_path = Path(file_path)
            result = write_moments_export(
                raw_root,
                source_path,
                usernames=usernames,
                export_date=export_date,
                staging_dir=staging_dir,
            )
            if result.missing_media_posts:
                marker = f"export_moments_media:{result.path.stem}"
                if marker not in self.partial_failures:
                    self.partial_failures.append(marker)
                print(
                    f"[WARN] 朋友圈有 {result.missing_media_posts} 条动态的媒体未解密，"
                    "已保留 URL 并继续导出。",
                    file=sys.stderr,
                )
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            try:
                staging_parent.rmdir()
            except OSError:
                pass

    def transcribe_voices(self, usernames: list[str]) -> None:
        # Inline in export_chats; deliberately absent from capabilities.
        return None

    def shutdown(self) -> None:
        # WeFlow remains user-owned; only the backend-owned ASR worker closes.
        if self._transcriber is not None and hasattr(self._transcriber, "close"):
            self._transcriber.close()

    def _probe_ready(self) -> None:
        self.client.health()
        self.client.validate_token()

    def _validate_configured_contacts_before_mutation(self) -> None:
        if not self._configured_contact_targets():
            return
        sessions, contacts = self._export_roster()
        self._validate_configured_contacts(sessions, contacts)

    def _export_roster(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if self._sessions_cache is None:
            self._sessions_cache = self.client.fetch_sessions(limit=2000)
        if self._contacts_cache is None:
            self._contacts_cache = self.client.fetch_contacts(limit=5000)
        return self._sessions_cache, self._contacts_cache

    def _configured_contact_targets(self) -> list[tuple[str, str]]:
        configured: list[tuple[str, str]] = []
        for config_key, values in (
            ("target_usernames", self.config.daily_export.target_usernames),
            ("self_moments_usernames", self.config.daily_export.self_moments_usernames),
        ):
            for value in values:
                target = str(value).strip()
                if target:
                    configured.append((config_key, target))
        return configured

    def _validate_configured_contacts(
        self,
        sessions: list[dict[str, Any]],
        contacts: list[dict[str, Any]],
    ) -> None:
        records = [*sessions, *contacts]
        for config_key, target in self._configured_contact_targets():
            matched_identities: set[str] = set()
            for record in records:
                identity = str(record.get("username") or record.get("wxid") or "").strip()
                if not identity:
                    continue
                candidates = {
                    str(record.get(field) or "").strip()
                    for field in ("username", "wxid", "displayName", "nickname", "remark", "alias")
                }
                if target in candidates:
                    matched_identities.add(identity)
            readable_target = _readable_error_summary(target, limit=60)
            if not matched_identities:
                raise RuntimeError(
                    f"配置 {config_key} 的联系人「{readable_target}」未在 WeFlow 名册中找到；"
                    "请更新为当前 wxid 或精确显示名后重试"
                )
            if len(matched_identities) > 1:
                raise RuntimeError(
                    f"配置 {config_key} 的联系人「{readable_target}」匹配到 {len(matched_identities)} 个账号；"
                    "请改用唯一 wxid 后重试"
                )

    def _asr(self) -> tuple[Any, str]:
        if self._transcriber_initialized:
            return self._transcriber, self._asr_unavailable_reason
        self._transcriber_initialized = True
        engine = self.config.asr.engine
        if not engine:
            self._asr_unavailable_reason = "ASR未启用"
        elif engine == "whisper":
            self._asr_unavailable_reason = "whisper引擎本期未就绪"
        elif engine == "sensevoice":
            worker_python = self.config.asr.worker_python
            if worker_python is None or not worker_python.is_file():
                self._asr_unavailable_reason = "SenseVoice worker_python未配置或不可执行"
            else:
                self._transcriber = SenseVoiceTranscriber(
                    worker_python=worker_python,
                    worker_script=self.config.asr.worker_script,
                    model=self.config.asr.model,
                    language=self.config.asr.language,
                    device=self.config.asr.device,
                    startup_timeout_sec=self.config.asr.worker_startup_timeout_sec,
                    request_timeout_sec=self.config.asr.worker_request_timeout_sec,
                )
                self._asr_unavailable_reason = "SenseVoice worker未就绪"
        else:
            self._asr_unavailable_reason = f"未知ASR引擎:{engine}"
        return self._transcriber, self._asr_unavailable_reason

    def _record_session_failure(self, talker: str, reason: str, display_name: str | None = None) -> None:
        marker = f"export_chat_session:{talker}"
        self.partial_failures.append(marker)
        if display_name and display_name != talker:
            session_label = f"会话「{display_name}」({talker})"
        else:
            session_label = f"会话 {talker}"
        print(f"[WARN] {session_label} 导出失败，已隔离: {reason}", file=sys.stderr)

    def _load_failure_state(self) -> SessionFailureState:
        state_path = self.config.base_dir / ".export-state.json"
        try:
            self._failure_state = SessionFailureState.load(state_path)
            self._failure_state_writable = True
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._failure_state = SessionFailureState(state_path)
            self._failure_state_writable = False
            print(
                f"[WARN] 无法读取会话失败状态，本轮不会覆盖该文件：{exc}",
                file=sys.stderr,
            )
        return self._failure_state

    def _save_failure_state(self) -> None:
        if self._failure_state is None or not self._failure_state_writable:
            return
        try:
            self._failure_state.save()
        except OSError as exc:
            self._failure_state_writable = False
            print(f"[WARN] 无法写入会话失败状态：{exc}", file=sys.stderr)

    @staticmethod
    def _record_session_success(
        state: SessionFailureState,
        talker: str,
        display_name: str,
    ) -> None:
        if state.record_success(talker):
            print(f"[INFO] 会话「{display_name}」已恢复正常导出，已从忽略名单移除")

    def _append_ignored_failure_detail(self, talker: str, display_name: str, reason: str) -> None:
        log_path = self.config.base_dir / ".runlog" / f"{date.today():%Y-%m-%d}-daily-export.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as stream:
                summary = " ".join(str(reason).split())[:500]
                stream.write(f"[IGNORED] 会话导出失败 {display_name} ({talker}): {summary}\n")
        except OSError:
            # The same detail remains in .export-state.json; logging must not
            # turn an explicitly ignored upstream failure back into a failure.
            pass


def _launch_weflow_normally(executable: Path) -> subprocess.Popen[Any]:
    return subprocess.Popen(
        [str(executable)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _readable_error_summary(value: str, limit: int = 180) -> str:
    text = " ".join(str(value or "未知错误").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _is_platform_account(talker: str) -> bool:
    return talker.startswith("gh_") or talker.endswith(_SKIPPED_ACCOUNT_SUFFIXES)


def _is_no_local_records_failure(
    exc: BaseException,
    session: dict[str, Any],
    export_date: date,
    *,
    historical_wxids: set[str],
    history_scan_complete: bool,
) -> tuple[bool, int | None]:
    """Return true only when all independent empty-session evidence agrees."""

    if not history_scan_complete or not isinstance(exc, WeflowApiError) or exc.status != 500:
        return False, None
    message = " ".join(str(exc).split())
    if (
        "/api/v1/messages" not in message
        or "消息数据库未找到" not in message
        or re.search(r"创建游标失败\s*[:：]\s*-3(?:\D|$)", message) is None
    ):
        return False, None
    talker = str(session.get("username") or "")
    if not talker or talker in historical_wxids:
        return False, None
    stale, last_timestamp = _stale_session_evidence(session, export_date)
    return stale, last_timestamp


def _stale_session_evidence(session: dict[str, Any], export_date: date) -> tuple[bool, int | None]:
    raw_timestamp = session.get("lastTimestamp")
    if raw_timestamp in (None, "", 0, "0"):
        return True, None
    if isinstance(raw_timestamp, bool):
        return False, None
    try:
        timestamp = int(raw_timestamp)
        if timestamp <= 0:
            return False, None
        if timestamp >= 100_000_000_000:
            timestamp //= 1000
        activity_date = datetime.fromtimestamp(timestamp).date()
    except (OSError, OverflowError, TypeError, ValueError):
        return False, None
    cutoff = export_date - timedelta(days=NO_LOCAL_RECORDS_STALE_DAYS)
    return activity_date <= cutoff, timestamp


def _historical_export_wxids(raw_archive: Path) -> tuple[set[str], bool]:
    """Read prior canonical chat identities; any unreadable candidate fails closed."""

    if not raw_archive.exists():
        return set(), True
    wxids: set[str] = set()
    try:
        paths = list(raw_archive.rglob("*.json"))
        for path in paths:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return set(), False
            session = payload.get("session")
            if session is None:
                continue
            if not isinstance(session, dict):
                return set(), False
            talker = str(session.get("wxid") or session.get("username") or "")
            if not talker:
                return set(), False
            wxids.add(talker)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return set(), False
    return wxids, True


__all__ = ["WeflowApiBackend"]
