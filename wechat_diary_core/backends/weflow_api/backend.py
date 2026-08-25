"""ExporterBackend implementation for the WeFlow 5.x local HTTP API."""

from __future__ import annotations

import json
import os
import re
import subprocess
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Sequence

from ...archiving import strip_date_suffix
from ...asr import SenseVoiceTranscriber
from ...config import Config
from ...media_store import MEDIA_STORE_NAME
from .bulk_state import BulkExportState, DateInterval
from .bulk_plan import BulkExportPlan
from .client import WeflowApiClient, WeflowApiError
from .failure_state import SessionFailureState
from .mapper import (
    ImageMediaStats,
    session_directory_display_name,
    session_directory_name,
    write_moments_export,
    write_session_export,
)


NO_LOCAL_RECORDS_STALE_DAYS = 365
_SKIPPED_ACCOUNT_SUFFIXES = ("@openim", "@opencustomerservicemsg")


@dataclass(frozen=True)
class BulkExportResult:
    requested_sessions: int
    completed_intervals: int
    resumed_intervals: int
    skipped_platform_accounts: int
    messages_written: int
    files_written: tuple[Path, ...]
    failures: dict[str, str]
    planned_sessions: int = 0
    resumed_plans: int = 0
    reconciliation_path: Path | None = None
    reconciliation_differences: int = 0


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
        self._prepare_api(validate_before_mutation=True)

    def prepare_bulk_export(self) -> None:
        """Prepare the API without applying the daily pre-rotation validation.

        Bulk export writes to its own ``--out`` root: the daily sidecar contact
        targets do not apply, and live-raw / archive collisions cannot occur.
        Directory collisions among the *selected* sessions are still checked, in
        ``export_chats_range``, through the same predicate the daily gate uses.
        """

        self._prepare_api(validate_before_mutation=False)

    def _prepare_api(self, *, validate_before_mutation: bool) -> None:
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
                    if validate_before_mutation:
                        self._validate_before_mutation()
                    return
            raise RuntimeError(
                "WeFlow API 服务未启动或鉴权失败。请打开 WeFlow → 设置 → API 服务 → "
                "启动服务，并确认 Access Token 已固定；修改 token 后需重启 API 服务。"
                f" 最后错误：{last_error}"
            ) from first_error
        else:
            if validate_before_mutation:
                self._validate_before_mutation()
            return

    def export_chats(self, export_date: date) -> None:
        self.export_chats_range(export_date, export_date, _daily_compat=True)

    def export_chats_range(
        self,
        start: date,
        end: date,
        *,
        raw_root: Path | None = None,
        sessions: Sequence[Mapping[str, Any]] | None = None,
        state_path: Path | None = None,
        force: bool = False,
        plan_path: Path | None = None,
        chunk_target: int = 10_000,
        replan: bool = False,
        _daily_compat: bool = False,
    ) -> BulkExportResult | None:
        """Export selected sessions over an inclusive range as one canonical file per active day."""

        if start > end:
            raise ValueError("批量导出的起始日期不能晚于结束日期")
        if _daily_compat:
            if start != end:
                raise ValueError("日常兼容路径只接受单日范围")
            self._export_chats_daily(start)
            return None
        if raw_root is None:
            raise ValueError("批量导出必须显式提供 raw_root")
        if state_path is None:
            raise ValueError("批量导出必须提供独立 state_path 以支持续跑")

        if chunk_target <= 0:
            raise ValueError("批量导出切块目标必须大于 0")
        destination_root = Path(raw_root).expanduser().resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        state_file = Path(state_path).expanduser()
        state = BulkExportState.load(state_file, destination_root)
        plan_file = Path(plan_path).expanduser() if plan_path is not None else state_file.with_name(
            f"{state_file.name}.plan.json"
        )
        plan = BulkExportPlan.load(plan_file, destination_root)
        self.partial_failures.clear()
        if sessions is None:
            selected_sessions, contacts = self._export_roster()
        else:
            selected_sessions = [dict(session) for session in sessions]
            if self._contacts_cache is None:
                self._contacts_cache = self.client.fetch_contacts(limit=5000)
            contacts = self._contacts_cache
        if not selected_sessions:
            raise RuntimeError("WeFlow API 没有返回可导出的会话，拒绝把空结果当成功")
        _validate_bulk_destination_names(
            selected_sessions,
            contacts=contacts,
            skip_platform_accounts=self.config.daily_export.skip_official_accounts,
        )

        planned_sessions = 0
        resumed_plans = 0
        plan_failures: dict[str, str] = {}
        for index, session in enumerate(selected_sessions, start=1):
            talker = str(session.get("username") or "")
            display_name = session_directory_display_name(session, contacts)
            if not talker or (
                self.config.daily_export.skip_official_accounts and _is_platform_account(talker)
            ):
                continue
            if replan:
                plan.invalidate(talker, start, end)
            pending_plan = plan.uncovered(talker, start, end)
            if not pending_plan:
                resumed_plans += 1
                continue
            planned_this_session = False
            for interval in pending_plan:
                print(
                    f"[INFO] [{index}/{len(selected_sessions)}] 规划会话「{display_name}」："
                    f"{interval.start} ~ {interval.end}",
                )
                try:
                    messages = self.client.fetch_messages(
                        talker,
                        start=interval.start,
                        end=interval.end,
                        media=False,
                    )
                    messages_by_day = _split_messages_by_day(messages, interval)
                    plan.record_completed(
                        talker,
                        display_name,
                        interval,
                        {day: len(day_messages) for day, day_messages in messages_by_day.items()},
                    )
                    planned_this_session = True
                except Exception as exc:
                    reason = _readable_error_summary(str(exc), limit=500)
                    plan_failures[talker] = reason
                    plan.record_failure(talker, display_name, interval, reason)
                    break
            if planned_this_session and talker not in plan_failures:
                planned_sessions += 1

        completed_intervals = 0
        resumed_intervals = 0
        skipped_platform_accounts = 0
        messages_written = 0
        files_written: list[Path] = []
        failures: dict[str, str] = dict(plan_failures)
        for index, session in enumerate(selected_sessions, start=1):
            talker = str(session.get("username") or "")
            display_name = session_directory_display_name(session, contacts)
            if not talker:
                failures[f"unknown-{index}"] = "会话缺少 username"
                self._record_session_failure("unknown", "会话缺少 username", display_name)
                continue
            if self.config.daily_export.skip_official_accounts and _is_platform_account(talker):
                skipped_platform_accounts += 1
                continue

            daily_plan = plan.daily_counts(talker, start, end)
            if daily_plan is None:
                reason = plan.failure_reason(talker) or "plan 未完整覆盖请求范围"
                failures[talker] = f"plan 不可用：{reason}"
                continue
            pending = state.uncovered(talker, start, end, force=force)
            if not pending:
                resumed_intervals += 1
                print(
                    f"[INFO] [{index}/{len(selected_sessions)}] 会话「{display_name}」"
                    f"已覆盖 {start} ~ {end}，跳过"
                )
                continue

            group_members: list[dict[str, Any]] | None = None
            for pending_interval in pending:
                chunks = _chunk_interval_by_messages(
                    pending_interval,
                    daily_plan,
                    target_messages=chunk_target,
                )
                _validate_interval_partition(pending_interval, chunks)
                for interval in chunks:
                    print(
                        f"[INFO] [{index}/{len(selected_sessions)}] 导出会话「{display_name}」："
                        f"{interval.start} ~ {interval.end}"
                    )
                    try:
                        messages = self.client.fetch_messages(
                        talker,
                        start=interval.start,
                        end=interval.end,
                        media=self.config.export_backend.weflow_api.media_localize,
                        )
                        messages_by_day = _split_messages_by_day(messages, interval)
                        if group_members is None:
                            group_members = (
                                self.client.fetch_group_members(talker)
                                if messages and talker.endswith("@chatroom")
                                else []
                            )
                        transcriber, reason = self._asr()
                        interval_files: list[Path] = []
                        interval_message_count = 0
                        image_media_stats = ImageMediaStats()
                        for export_day in sorted(messages_by_day):
                            day_messages = messages_by_day[export_day]
                            published = write_session_export(
                            destination_root,
                            session,
                            day_messages,
                            start=export_day,
                            end=export_day,
                            contacts=contacts,
                            group_members=group_members,
                            self_wxids=self.config.user.self_wxids,
                            transcriber=transcriber,
                            asr_unavailable_reason=reason,
                            emit_emotion=self.config.asr.emit_emotion,
                            require_media=self.config.export_backend.weflow_api.media_localize,
                            appmsg_text_max_chars=self.config.export_backend.weflow_api.appmsg_text_max_chars,
                            image_media_stats=image_media_stats,
                            )
                            interval_files.extend(path for path in published.rglob("*") if path.is_file())
                            interval_files.extend(_referenced_cas_files(published, destination_root))
                            interval_message_count += len(day_messages)

                        state.record_completed(
                            talker,
                            display_name,
                            interval,
                            _unique_paths(interval_files),
                            daily_counts={day: len(day_messages) for day, day_messages in messages_by_day.items()},
                        )
                        completed_intervals += 1
                        messages_written += interval_message_count
                        files_written.extend(_unique_paths(interval_files))
                        if image_media_stats.missing_image_paths or image_media_stats.missing_image_files:
                            marker = (
                            f"export_chat_image_media:{talker}:"
                            f"missing_path={image_media_stats.missing_image_paths}:"
                            f"missing_file={image_media_stats.missing_image_files}"
                            )
                            self.partial_failures.append(marker)
                            print(
                            f"[WARN] 会话「{_readable_error_summary(display_name, limit=60)}」"
                            "有图片降级为 [图片]："
                            f"缺少本地路径 {image_media_stats.missing_image_paths} 条；"
                            f"路径指向的文件不存在 {image_media_stats.missing_image_files} 条。"
                            "已继续导出。",
                                file=sys.stderr,
                            )
                        if image_media_stats.missing_video_md5 or image_media_stats.missing_video_files:
                            marker = (
                            f"export_chat_video_media:{talker}:"
                            f"missing_md5={image_media_stats.missing_video_md5}:"
                            f"missing_file={image_media_stats.missing_video_files}"
                            )
                            self.partial_failures.append(marker)
                            print(
                            f"[WARN] 会话「{_readable_error_summary(display_name, limit=60)}」"
                            "有视频降级为 [视频]："
                            f"缺少 videoMd5 {image_media_stats.missing_video_md5} 条；"
                            f"有 md5 但本地文件不存在 {image_media_stats.missing_video_files} 条。"
                            "已继续导出。",
                                file=sys.stderr,
                            )
                    except Exception as exc:
                        reason = _readable_error_summary(str(exc), limit=500)
                        failures[talker] = reason
                        state.record_failure(talker, display_name, interval, reason)
                        self._record_session_failure(talker, reason, display_name)
                        break

                if talker in failures:
                    break

        if skipped_platform_accounts:
            print(
                f"[INFO] 已跳过 {skipped_platform_accounts} 个公众号/企业微信/客服会话"
                "（skip_official_accounts=true）"
            )
        reconciliation_path, reconciliation_differences = _write_bulk_reconciliation(
            destination_root,
            plan,
            state,
            selected_sessions,
            contacts=contacts,
            start=start,
            end=end,
            skip_platform_accounts=self.config.daily_export.skip_official_accounts,
        )
        return BulkExportResult(
            requested_sessions=len(selected_sessions),
            completed_intervals=completed_intervals,
            resumed_intervals=resumed_intervals,
            skipped_platform_accounts=skipped_platform_accounts,
            messages_written=messages_written,
            files_written=tuple(files_written),
            failures=failures,
            planned_sessions=planned_sessions,
            resumed_plans=resumed_plans,
            reconciliation_path=reconciliation_path,
            reconciliation_differences=reconciliation_differences,
        )

    def _export_chats_daily(self, export_date: date) -> None:
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
            display_name = session_directory_display_name(session, contacts)
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
                if image_media_stats.missing_video_md5 or image_media_stats.missing_video_files:
                    marker = (
                        f"export_chat_video_media:{talker}:"
                        f"missing_md5={image_media_stats.missing_video_md5}:"
                        f"missing_file={image_media_stats.missing_video_files}"
                    )
                    self.partial_failures.append(marker)
                    readable_name = _readable_error_summary(display_name, limit=60)
                    print(
                        f"[WARN] 会话「{readable_name}」有视频降级为 [视频]："
                        f"缺少 videoMd5 {image_media_stats.missing_video_md5} 条；"
                        f"有 md5 但本地文件不存在 {image_media_stats.missing_video_files} 条。已继续导出。",
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
            if result.extension_conflicts:
                print(
                    f"[WARN] 朋友圈媒体有 {result.extension_conflicts} 个同 md5 不同扩展名，"
                    "已复用 store 中既有文件。",
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

    def _validate_before_mutation(self) -> None:
        self._validate_configured_contacts_before_mutation()
        sessions, contacts = self._export_roster()
        current_collisions = _current_session_directory_collisions(
            sessions,
            contacts=contacts,
            skip_official_accounts=self.config.daily_export.skip_official_accounts,
        )
        live_raw_collisions = _stored_session_directory_collisions(
            self.config.paths.raw,
            directory_key=strip_date_suffix,
        )
        archive_collisions = _stored_session_directory_collisions(self.config.paths.archived / "raw")
        if current_collisions or live_raw_collisions or archive_collisions:
            details: list[str] = []
            if current_collisions:
                details.append("本轮会话：" + _format_session_directory_collisions(current_collisions))
            if live_raw_collisions:
                details.append("待轮转 live raw：" + _format_session_directory_collisions(live_raw_collisions))
            if archive_collisions:
                details.append("已有归档：" + _format_session_directory_collisions(archive_collisions))
            raise RuntimeError(
                "检测到不同 wxid 会写入同一 canonical 会话目录；已在任何归档轮转或 live 写入前拒绝本轮导出。 "
                + " ".join(details)
                + " 请去微信里改其中一方的备注，使双方显示名净化后不同，再重新运行导出。"
                + (
                    " 既有 live raw 或归档冲突不会被改备注自动修复，请先保留其目录并另行人工核对。"
                    if live_raw_collisions or archive_collisions
                    else ""
                )
            )

    def _validate_configured_contacts_before_mutation(self) -> None:
        if not self._configured_contact_targets():
            return
        sessions, contacts = self._export_roster()
        self._validate_configured_contacts(sessions, contacts)

    def _sessions(self) -> list[dict[str, Any]]:
        if self._sessions_cache is None:
            self._sessions_cache = self.client.fetch_sessions(limit=10000)
        return self._sessions_cache

    def _export_roster(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        sessions = self._sessions()
        if self._contacts_cache is None:
            self._contacts_cache = self.client.fetch_contacts(limit=10000)
        return sessions, self._contacts_cache

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


def _current_session_directory_collisions(
    sessions: list[dict[str, Any]],
    *,
    contacts: Sequence[Mapping[str, Any]],
    skip_official_accounts: bool,
) -> dict[str, dict[str, str]]:
    """Return only canonical daily directories claimed by multiple wxids."""

    candidates: dict[str, dict[str, str]] = {}
    today = date.today()
    for session in sessions:
        wxid = str(session.get("username") or "").strip()
        if not wxid or (skip_official_accounts and _is_platform_account(wxid)):
            continue
        directory = session_directory_name(session, today, today, contacts=contacts)
        candidates.setdefault(directory, {}).setdefault(
            wxid,
            session_directory_display_name(session, contacts),
        )
    return {
        directory: identities
        for directory, identities in candidates.items()
        if len(identities) > 1
    }


def _stored_session_directory_collisions(
    raw_root: Path,
    *,
    directory_key: Callable[[str], str] = str,
) -> dict[str, dict[str, str]]:
    """Find preserved evidence that one archive key contains multiple wxids.

    We inspect only direct JSON children of first-level session directories, the
    same shape used by the rename reconciler. ``directory_key`` is identity for
    archived directories and strips the date suffix for live raw, matching the
    rotation destination exactly. Root-level moments exports and media subtrees
    therefore cannot participate in this chat-identity guard.
    """

    if not raw_root.is_dir():
        return {}
    candidates: dict[str, dict[str, str]] = {}
    for session_dir in sorted(
        path
        for path in raw_root.iterdir()
        if path.is_dir() and path.name != MEDIA_STORE_NAME
    ):
        directory = directory_key(session_dir.name)
        identities = candidates.setdefault(directory, {})
        for json_path in sorted(session_dir.glob("*.json")):
            session = _read_stored_session(json_path)
            if session is None:
                continue
            wxid = str(session.get("wxid") or session.get("username") or "").strip()
            if wxid:
                identities.setdefault(wxid, _session_display_name(session, wxid))
    return {
        directory: identities
        for directory, identities in candidates.items()
        if len(identities) > 1
    }


_SESSION_PREFIX_CHUNK_CHARS = 4096
_SESSION_PREFIX_MAX_CHARS = 65536
_NEED_MORE_PREFIX = object()
_USE_FULL_DOCUMENT = object()


def _read_stored_session(json_path: Path) -> dict[str, Any] | None:
    """Read the leading canonical session object without materializing messages.

    Canonical exports serialize ``session`` as the first top-level field. Any
    other ordering, oversized metadata, or ambiguous prefix falls back to the
    previous full-document parser so the optimization cannot hide a supported
    historical shape.
    """

    try:
        with json_path.open("r", encoding="utf-8") as stream:
            prefix = ""
            while len(prefix) < _SESSION_PREFIX_MAX_CHARS:
                chunk = stream.read(
                    min(
                        _SESSION_PREFIX_CHUNK_CHARS,
                        _SESSION_PREFIX_MAX_CHARS - len(prefix),
                    )
                )
                prefix += chunk
                parsed = _parse_leading_session(prefix)
                if isinstance(parsed, dict):
                    return parsed
                if parsed is _USE_FULL_DOCUMENT or not chunk:
                    break
    except (OSError, UnicodeDecodeError):
        return None

    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("session"), dict):
        return None
    return payload["session"]


def _parse_leading_session(prefix: str) -> dict[str, Any] | object:
    """Return a complete first ``session`` value or a parser control sentinel."""

    decoder = json.JSONDecoder()
    index = 0
    while index < len(prefix) and prefix[index].isspace():
        index += 1
    if index >= len(prefix):
        return _NEED_MORE_PREFIX
    if prefix[index] != "{":
        return _USE_FULL_DOCUMENT
    index += 1
    while index < len(prefix) and prefix[index].isspace():
        index += 1
    try:
        key, index = decoder.raw_decode(prefix, index)
    except json.JSONDecodeError:
        return _NEED_MORE_PREFIX
    if key != "session":
        return _USE_FULL_DOCUMENT
    while index < len(prefix) and prefix[index].isspace():
        index += 1
    if index >= len(prefix):
        return _NEED_MORE_PREFIX
    if prefix[index] != ":":
        return _USE_FULL_DOCUMENT
    index += 1
    while index < len(prefix) and prefix[index].isspace():
        index += 1
    try:
        session, _ = decoder.raw_decode(prefix, index)
    except json.JSONDecodeError:
        return _NEED_MORE_PREFIX
    return session if isinstance(session, dict) else _USE_FULL_DOCUMENT


def _session_display_name(session: dict[str, Any], wxid: str) -> str:
    display_name = " ".join(str(session.get("displayName") or "").split())
    return display_name or f"（缺失，目录名回退为 {wxid}）"


def _format_session_directory_collisions(collisions: dict[str, dict[str, str]]) -> str:
    groups = []
    for directory, identities in sorted(collisions.items()):
        participants = "; ".join(
            f"wxid={wxid}, displayName=「{display_name}」"
            for wxid, display_name in sorted(identities.items())
        )
        groups.append(f"目录「{directory}」：{participants}。")
    return " ".join(groups)


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


def _split_messages_by_day(
    messages: Sequence[Mapping[str, Any]],
    interval: DateInterval,
) -> dict[date, list[Mapping[str, Any]]]:
    grouped: dict[date, list[Mapping[str, Any]]] = {}
    for message in messages:
        raw_timestamp = message.get("createTime")
        if isinstance(raw_timestamp, bool):
            raise ValueError("WeFlow API 消息 createTime 无效，无法按天切分")
        try:
            timestamp = int(raw_timestamp)
            if timestamp >= 100_000_000_000:
                timestamp //= 1000
            if timestamp <= 0:
                raise ValueError
            message_day = datetime.fromtimestamp(timestamp).date()
        except (OSError, OverflowError, TypeError, ValueError) as exc:
            raise ValueError("WeFlow API 消息 createTime 无效，无法按天切分") from exc
        if not interval.start <= message_day <= interval.end:
            raise ValueError(
                "WeFlow API 返回了请求范围外的消息，拒绝静默丢弃："
                f"createTime={raw_timestamp}，日期={message_day}，"
                f"请求={interval.start}~{interval.end}"
            )
        grouped.setdefault(message_day, []).append(message)
    return grouped


def _chunk_interval_by_messages(
    interval: DateInterval,
    daily_counts: Mapping[date, int],
    *,
    target_messages: int,
) -> list[DateInterval]:
    """Partition every calendar day, keeping an oversized day atomic."""

    if target_messages <= 0:
        raise ValueError("批量导出切块目标必须大于 0")
    chunks: list[DateInterval] = []
    chunk_start = interval.start
    chunk_count = 0
    current = interval.start
    while current <= interval.end:
        if current not in daily_counts:
            raise ValueError(f"plan 缺少逐日消息数：{current}")
        count = daily_counts[current]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"plan 的逐日消息数无效：{current}={count!r}")
        if count > target_messages:
            if chunk_start < current:
                chunks.append(DateInterval(chunk_start, current - timedelta(days=1)))
            chunks.append(DateInterval(current, current))
            chunk_start = current + timedelta(days=1)
            chunk_count = 0
        elif chunk_count and chunk_count + count > target_messages:
            chunks.append(DateInterval(chunk_start, current - timedelta(days=1)))
            chunk_start = current
            chunk_count = count
        else:
            chunk_count += count
        current += timedelta(days=1)
    if chunk_start <= interval.end:
        chunks.append(DateInterval(chunk_start, interval.end))
    return chunks


def _validate_interval_partition(parent: DateInterval, chunks: Sequence[DateInterval]) -> None:
    """Reject gaps, overlaps, inversions, and boundaries escaping ``parent``."""

    if not chunks:
        raise ValueError(f"切块为空，无法覆盖请求区间：{parent.start}~{parent.end}")
    cursor = parent.start
    for index, chunk in enumerate(chunks, start=1):
        if chunk.start > chunk.end:
            raise ValueError(f"切块 {index} 起止倒置：{chunk.start}~{chunk.end}")
        if chunk.start < parent.start or chunk.end > parent.end:
            raise ValueError(f"切块 {index} 越出请求区间：{chunk.start}~{chunk.end}")
        if chunk.start != cursor:
            kind = "重叠" if chunk.start < cursor else "缺口"
            raise ValueError(
                f"切块 {kind}：期望从 {cursor} 开始，实际为 {chunk.start}~{chunk.end}"
            )
        cursor = chunk.end + timedelta(days=1)
    if cursor != parent.end + timedelta(days=1):
        raise ValueError(f"切块缺口：最后覆盖到 {cursor - timedelta(days=1)}，应到 {parent.end}")


def _write_bulk_reconciliation(
    raw_root: Path,
    plan: BulkExportPlan,
    state: BulkExportState,
    sessions: Sequence[Mapping[str, Any]],
    *,
    contacts: Sequence[Mapping[str, Any]],
    start: date,
    end: date,
    skip_platform_accounts: bool,
) -> tuple[Path, int]:
    """Write an inspectable plan-vs-actual report; drift is evidence, not failure."""

    entries: list[dict[str, Any]] = []
    difference_count = 0
    for index, session in enumerate(sessions, start=1):
        talker = str(session.get("username") or "")
        display_name = session_directory_display_name(session, contacts)
        entry: dict[str, Any] = {
            "session": talker or f"unknown-{index}",
            "displayName": display_name,
            "range": {"start": start.isoformat(), "end": end.isoformat()},
        }
        if not talker:
            entry["status"] = "invalid_session"
            entry["reason"] = "会话缺少 username"
            entries.append(entry)
            continue
        if skip_platform_accounts and _is_platform_account(talker):
            entry["status"] = "skipped_platform_account"
            entries.append(entry)
            continue
        planned = plan.daily_counts(talker, start, end)
        if planned is None:
            entry["status"] = "plan_unavailable"
            entry["reason"] = plan.failure_reason(talker) or "plan 未完整覆盖请求范围"
            entries.append(entry)
            continue
        actual = state.completed_daily_counts(talker, start, end)
        covered = state.covered_days(talker, start, end)
        differences: list[dict[str, Any]] = []
        for current in _days_inclusive(start, end):
            expected = planned[current]
            observed = actual.get(current) if actual is not None else None
            if expected != observed:
                differences.append(
                    {"day": current.isoformat(), "plannedMessages": expected, "exportedMessages": observed}
                )
        entry.update(
            {
                "plannedMessages": sum(planned.values()),
                "plannedActiveDays": sum(1 for value in planned.values() if value),
                "plannedDailyCounts": {day.isoformat(): planned[day] for day in sorted(planned)},
                "exportedMessages": sum(actual.values()) if actual is not None else None,
                "exportedActiveDays": sum(1 for value in actual.values() if value) if actual is not None else None,
                "exportedDailyCounts": (
                    {day.isoformat(): actual[day] for day in sorted(actual)} if actual is not None else None
                ),
                "exportedCoverageDays": len(covered),
                "differences": differences,
            }
        )
        entry["status"] = (
            "matched"
            if not differences and len(covered) == (end - start).days + 1
            else "drift_or_incomplete"
        )
        difference_count += len(differences)
        entries.append(entry)
    path = raw_root.parent / f".{raw_root.name}.bulk-export-reconciliation.json"
    payload = {
        "version": 1,
        "outputRoot": str(raw_root.resolve()),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "sessions": entries,
        "differenceCount": difference_count,
    }
    _write_json_atomically(path, payload)
    return path, difference_count


def _write_json_atomically(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _days_inclusive(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _referenced_cas_files(session_dir: Path, raw_root: Path) -> list[Path]:
    """Resolve every non-empty chat media reference for state/output tracking."""

    resolved_root = raw_root.resolve()
    files: list[Path] = []
    for json_path in sorted(session_dir.glob("*.json")):
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        for message in payload.get("messages") or []:
            if not isinstance(message, Mapping):
                continue
            reference = str(message.get("source") or "").strip()
            if not reference:
                continue
            candidate = (json_path.parent / Path(reference)).resolve()
            try:
                candidate.relative_to(resolved_root)
            except ValueError as exc:
                raise ValueError(f"canonical 媒体引用越出导出根：{reference}") from exc
            if not candidate.is_file():
                raise FileNotFoundError(f"canonical 媒体引用不存在：{reference}")
            files.append(candidate)
    return _unique_paths(files)


def _unique_paths(paths: Sequence[Path]) -> list[Path]:
    return list(dict.fromkeys(Path(path) for path in paths))


def _validate_bulk_destination_names(
    sessions: Sequence[Mapping[str, Any]],
    *,
    contacts: Sequence[Mapping[str, Any]],
    skip_platform_accounts: bool,
) -> None:
    """Reject a bulk run whose selected sessions would share one directory.

    Reuses the daily gate's predicate so one definition decides what counts as
    a collision. Only the "current sessions" face applies: bulk writes to its
    own ``--out`` root, so the live-raw and archive faces are unreachable here.
    The date carried by a directory name is identical for every session in one
    run, so the predicate's internal date is irrelevant to the verdict.
    """

    collisions = _current_session_directory_collisions(
        [dict(session) for session in sessions],
        contacts=contacts,
        skip_official_accounts=skip_platform_accounts,
    )
    if not collisions:
        return
    raise RuntimeError(
        "检测到不同 wxid 会写入同一 canonical 会话目录；已在任何写入前拒绝本次批量导出。 "
        "本次选中的会话："
        + _format_session_directory_collisions(collisions)
        + " 请去微信里改其中一方的备注，使双方显示名净化后不同，再重新运行；"
        "也可以用 --session 把它们分开导出到不同的 --out 根。"
    )


__all__ = ["BulkExportResult", "WeflowApiBackend"]
