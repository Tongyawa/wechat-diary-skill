from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch

from scripts import export_bulk_raw
from wechat_diary_core.backends.weflow_api.backend import (
    BulkExportResult,
    WeflowApiBackend,
    _chunk_interval_by_messages,
    _validate_interval_partition,
    _write_bulk_reconciliation,
)
from wechat_diary_core.backends.weflow_api.bulk_plan import BulkExportPlan
from wechat_diary_core.backends.weflow_api.bulk_state import BulkExportState, DateInterval
from wechat_diary_core.backends.weflow_api.client import WeflowApiClient, WeflowApiError
from wechat_diary_core.config import load_config
from wechat_diary_core.raw_schema import validate_session_json


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "weflow_api"


def _config(
    root: Path,
    *,
    target_usernames: tuple[str, ...] = (),
    media_localize: bool = False,
    bulk_start: str = "2010-01-01",
    bulk_end: str = "",
    excluded_session_usernames: tuple[str, ...] = (),
):
    path = root / "config.toml"
    path.write_text(
        f"""
[user]
self_wxids = ["wxid_self_placeholder"]

[paths]
raw = "{(root / 'daily-raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
archived = "{(root / 'archived').as_posix()}"
insights = "{(root / 'insights').as_posix()}"

[export_backend]
backend = "weflow_api"

[export_backend.weflow_api]
base_url = "http://127.0.0.1:5031"
access_token = "fixed-token"
media_localize = {str(media_localize).lower()}

[daily_export]
target_usernames = {json.dumps(list(target_usernames), ensure_ascii=False)}
skip_official_accounts = true
excluded_session_usernames = {json.dumps(list(excluded_session_usernames), ensure_ascii=False)}

[bulk_export]
start = "{bulk_start}"
end = "{bulk_end}"

[asr]
engine = ""
""".strip(),
        encoding="utf-8",
    )
    return load_config(path)


def _seed_driver_plan(raw_root: Path, start: date, end: date) -> Path:
    plan_path = export_bulk_raw.bulk_plan_path_for(raw_root)
    plan = BulkExportPlan.load(plan_path, raw_root)
    plan.resolve_range(start, end, allow_expand=False)
    plan.save()
    return plan_path


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _timestamp(day: date, hour: int) -> int:
    return int(datetime.combine(day, time(hour=hour)).timestamp())


def _message_for(day: date, local_id: int, *, hour: int = 12) -> dict:
    message = dict(_fixture("messages.json")["messages"][0])
    message.update(
        {
            "localId": local_id,
            "serverId": f"server-{local_id}",
            "createTime": _timestamp(day, hour),
            "sortSeq": local_id,
            "content": f"message {local_id}",
        }
    )
    return message


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class _BulkClient:
    def __init__(self, sessions: list[dict], messages: dict[str, list[dict]]) -> None:
        self.sessions = sessions
        self.messages = messages
        self.message_calls: list[tuple[str, date, date]] = []
        self.health_calls = 0
        self.token_calls = 0

    def health(self):
        self.health_calls += 1

    def validate_token(self):
        self.token_calls += 1

    def fetch_sessions(self, *, limit):
        return self.sessions

    def fetch_contacts(self, *, limit):
        return [
            {
                "username": session["username"],
                "displayName": session.get("displayName", session["username"]),
                "nickname": session.get("displayName", session["username"]),
            }
            for session in self.sessions
        ]

    def fetch_messages(self, talker, *, start, end, media, **_kwargs):
        self.message_calls.append((talker, start, end))
        return [
            message
            for message in self.messages.get(talker, [])
            if start <= datetime.fromtimestamp(int(message["createTime"])).date() <= end
        ]

    def fetch_group_members(self, talker):
        return []


class BulkExportTests(unittest.TestCase):
    def test_export_chats_delegates_to_single_day_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = WeflowApiBackend(_config(Path(tmp)))
            export_day = date(2026, 8, 5)
            with patch.object(backend, "export_chats_range") as export_range:
                backend.export_chats(export_day)

        export_range.assert_called_once_with(export_day, export_day, _daily_compat=True)

    def test_bulk_prepare_does_not_apply_daily_contact_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root, target_usernames=("missing-daily-target",))
            client = _BulkClient([], {})
            backend = WeflowApiBackend(cfg)
            backend._client = client

            backend.prepare_bulk_export()

        self.assertEqual(client.health_calls, 1)
        self.assertEqual(client.token_calls, 1)

    def test_real_descending_fixture_is_paginated_losslessly_and_written_in_order(self) -> None:
        fixture = _fixture("messages_descending.json")
        first_page = fixture["messages"][:2]
        second_page = fixture["messages"][2:]
        queries: list[dict[str, list[str]]] = []

        def opener(request, timeout):
            query = parse_qs(urlparse(request.full_url).query)
            queries.append(query)
            offset = int(query["offset"][0])
            page = first_page if offset == 0 else second_page
            return _Response(
                {
                    "success": True,
                    "talker": "wxid_contact_placeholder",
                    # 真机契约：count 是本页条数，不是声明总数（6 页实测
                    # 10000×5 后末页 6276，逐页 count == len(page)）。
                    "count": len(page),
                    "hasMore": offset == 0,
                    "messages": page,
                }
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            raw_root = root / "bulk-raw"
            state_path = root / ".bulk-state.json"
            backend = WeflowApiBackend(cfg)
            backend._client = WeflowApiClient(
                "http://127.0.0.1:5031",
                "fixed-token",
                opener=opener,
            )
            backend._contacts_cache = _fixture("contacts.json")["contacts"]
            session = _fixture("sessions.json")["sessions"][0]

            result = backend.export_chats_range(
                date(1970, 1, 1),
                date(1970, 1, 1),
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
            )
            export = next(raw_root.glob("私聊_示例联系人_19700101/*.json"))
            payload = json.loads(export.read_text(encoding="utf-8"))

        self.assertIsInstance(result, BulkExportResult)
        self.assertEqual([query["offset"] for query in queries], [["0"], ["2"], ["0"], ["2"]])
        self.assertTrue(all(query["limit"] == ["10000"] for query in queries))
        self.assertEqual(result.messages_written, len(fixture["messages"]))
        expected_ids = {str(message["serverId"]) for message in fixture["messages"]}
        actual_ids = {message["platformMessageId"] for message in payload["messages"]}
        self.assertEqual(actual_ids, expected_ids)
        create_times = [message["createTime"] for message in payload["messages"]]
        self.assertEqual(create_times, sorted(create_times))
        self.assertEqual(
            [message["platformMessageId"] for message in payload["messages"]],
            ["server-101", "server-102", "server-103", "server-104"],
        )
        validate_session_json(payload)

    def test_range_splits_cross_year_days_without_creating_empty_day(self) -> None:
        first = date(2025, 12, 31)
        second = date(2026, 1, 1)
        empty = date(2026, 1, 2)
        session = {"username": "wxid_range_placeholder", "displayName": "范围会话占位"}
        messages = [
            _message_for(second, 3, hour=20),
            _message_for(first, 2, hour=23),
            _message_for(first, 1, hour=1),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            raw_root = root / "bulk-raw"
            backend = WeflowApiBackend(cfg)
            backend._client = _BulkClient([session], {session["username"]: messages})

            result = backend.export_chats_range(
                first,
                empty,
                raw_root=raw_root,
                sessions=[session],
                state_path=root / ".bulk-state.json",
            )
            exports = sorted(raw_root.glob("*/*.json"))
            payloads = [json.loads(path.read_text(encoding="utf-8")) for path in exports]

        self.assertEqual([path.parent.name[-8:] for path in exports], ["20251231", "20260101"])
        self.assertEqual([payload["session"]["messageCount"] for payload in payloads], [2, 1])
        self.assertEqual(result.messages_written, 3)
        self.assertFalse(any(empty.strftime("%Y%m%d") in str(path) for path in exports))

    def test_same_type_and_display_name_collision_fails_before_any_message_request(self) -> None:
        export_day = date(2026, 8, 5)
        sessions = [
            {"username": "wxid_collision_a", "displayName": "同名会话占位"},
            {"username": "wxid_collision_b", "displayName": "同名会话占位"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            client = _BulkClient(sessions, {})
            backend = WeflowApiBackend(cfg)
            backend._client = client

            with self.assertRaises(RuntimeError) as raised:
                backend.export_chats_range(
                    export_day,
                    export_day,
                    raw_root=root / "bulk-raw",
                    sessions=sessions,
                    state_path=root / ".bulk-state.json",
                )

        # 锁语义而非措辞；并锁住项目硬规矩「守卫报错必须带上全部参与判断的值」——
        # 只印一半会把排查引向错误方向，所以两个 wxid 都必须出现在消息里。
        message = str(raised.exception)
        self.assertIn("同一 canonical 会话目录", message)
        self.assertIn("wxid_collision_a", message)
        self.assertIn("wxid_collision_b", message)
        self.assertEqual(client.message_calls, [])

    def test_excluded_real_shape_session_with_historical_completed_plan_is_neutral(self) -> None:
        export_day = date(2026, 8, 5)
        session = _fixture("sessions.json")["sessions"][0]
        talker = session["username"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "bulk-raw"
            plan_path = root / ".bulk-plan.json"
            plan = BulkExportPlan.load(plan_path, raw_root)
            plan.resolve_range(export_day, export_day, allow_expand=False)
            plan.record_completed(
                talker,
                session["displayName"],
                DateInterval(export_day, export_day),
                {export_day: 3},
            )
            plan.save()
            client = _BulkClient([session], {talker: [_message_for(export_day, 1)]})
            backend = WeflowApiBackend(
                _config(root, excluded_session_usernames=(talker,))
            )
            backend._client = client

            result = backend.export_chats_range(
                export_day,
                export_day,
                raw_root=raw_root,
                sessions=[session],
                state_path=root / ".bulk-state.json",
                plan_path=plan_path,
            )
            reconciliation = json.loads(
                result.reconciliation_path.read_text(encoding="utf-8")
            )
            preserved_plan = BulkExportPlan.load(plan_path, raw_root)

        self.assertEqual(client.message_calls, [])
        self.assertEqual(result.failures, {})
        self.assertEqual(result.skipped_excluded_sessions, 1)
        self.assertEqual(result.reconciliation_differences, 0)
        self.assertEqual(result.reconciliation_incomplete_coverage_days, 0)
        self.assertEqual(reconciliation["sessions"][0]["status"], "excluded_session")
        self.assertNotEqual(reconciliation["sessions"][0]["status"], "plan_unavailable")
        self.assertTrue(preserved_plan.has_completed(talker))

    def test_interruption_then_resume_skips_completed_session_and_matches_clean_run(self) -> None:
        export_day = date(2026, 8, 5)
        sessions = [
            {"username": "wxid_first_placeholder", "displayName": "第一会话占位"},
            {"username": "wxid_second_placeholder", "displayName": "第二会话占位"},
        ]
        messages = {
            sessions[0]["username"]: [_message_for(export_day, 1)],
            sessions[1]["username"]: [_message_for(export_day, 2)],
        }

        class InterruptingClient(_BulkClient):
            second_calls = 0

            def fetch_messages(self, talker, *, start, end, **kwargs):
                if talker == "wxid_second_placeholder":
                    self.second_calls += 1
                if talker == "wxid_second_placeholder" and self.second_calls == 2:
                    self.message_calls.append((talker, start, end))
                    raise KeyboardInterrupt("injected interruption")
                return super().fetch_messages(talker, start=start, end=end, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            resumed_root = root / "resumed"
            resumed_state = root / ".resumed-state.json"
            first_client = InterruptingClient(sessions, messages)
            backend = WeflowApiBackend(cfg)
            backend._client = first_client
            with self.assertRaisesRegex(KeyboardInterrupt, "injected"):
                backend.export_chats_range(
                    export_day,
                    export_day,
                    raw_root=resumed_root,
                    sessions=sessions,
                    state_path=resumed_state,
                )

            second_client = _BulkClient(sessions, messages)
            backend = WeflowApiBackend(cfg)
            backend._client = second_client
            resumed = backend.export_chats_range(
                export_day,
                export_day,
                raw_root=resumed_root,
                sessions=sessions,
                state_path=resumed_state,
            )

            clean_root = root / "clean"
            clean_client = _BulkClient(sessions, messages)
            backend = WeflowApiBackend(cfg)
            backend._client = clean_client
            backend.export_chats_range(
                export_day,
                export_day,
                raw_root=clean_root,
                sessions=sessions,
                state_path=root / ".clean-state.json",
            )

            resumed_tree = _tree_bytes(resumed_root)
            clean_tree = _tree_bytes(clean_root)

        self.assertEqual(
            [call[0] for call in second_client.message_calls],
            ["wxid_second_placeholder"],
        )
        self.assertEqual(resumed.resumed_intervals, 1)
        self.assertEqual(resumed_tree, clean_tree)

    def test_resume_state_tracks_cas_references_and_refetches_when_media_is_deleted(self) -> None:
        export_day = date(2026, 8, 5)
        session = {"username": "wxid_media_placeholder", "displayName": "媒体会话占位"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "中文图片.jpg"
            source.write_bytes(b"bulk-cas-image-placeholder")
            message = _message_for(export_day, 1)
            message.update(
                {
                    "localType": 3,
                    "content": "[图片]",
                    "mediaLocalPath": str(source),
                    "mediaFileName": source.name,
                    "mediaType": "image",
                }
            )
            cfg = _config(root)
            raw_root = root / "bulk-raw"
            state_path = root / ".bulk-state.json"
            first = WeflowApiBackend(cfg)
            first._client = _BulkClient([session], {session["username"]: [message]})
            first.export_chats_range(
                export_day,
                export_day,
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            recorded = state["sessions"][session["username"]]["completed"][0]["files"]
            self.assertTrue(recorded and all(value.endswith(".json") for value in recorded))
            session_dir = next(
                path for path in raw_root.iterdir() if path.is_dir() and path.name != "_media"
            )
            payload = json.loads(next(session_dir.glob("*.json")).read_text(encoding="utf-8"))
            media_path = (session_dir / payload["messages"][0]["source"]).resolve()
            media_path.unlink()

            second_client = _BulkClient([session], {session["username"]: [message]})
            second = WeflowApiBackend(cfg)
            second._client = second_client
            real_replace = os.replace
            publish_attempts = 0

            def replace_with_transient_lock(source_path, destination_path):
                nonlocal publish_attempts
                source_path = Path(source_path)
                destination_path = Path(destination_path)
                if (
                    source_path.parent.name == ".bulk-raw.weflow-api-staging"
                    and destination_path == session_dir
                ):
                    publish_attempts += 1
                    if publish_attempts == 1:
                        raise PermissionError("injected transient directory lock")
                return real_replace(source_path, destination_path)

            with (
                patch(
                    "wechat_diary_core.backends.weflow_api.mapper.os.replace",
                    side_effect=replace_with_transient_lock,
                ),
                patch("wechat_diary_core.backends.weflow_api.mapper.time.sleep") as retry_sleep,
            ):
                result = second.export_chats_range(
                    export_day,
                    export_day,
                    raw_root=raw_root,
                    sessions=[session],
                    state_path=state_path,
                )

            self.assertEqual(len(second_client.message_calls), 1)
            self.assertEqual(result.completed_intervals, 1)
            self.assertEqual(result.failures, {})
            self.assertEqual(publish_attempts, 2)
            retry_sleep.assert_called_once_with(0.01)
            self.assertTrue(media_path.is_file())
            self.assertIn(media_path, result.files_written)

    def test_resume_state_allows_degraded_image_without_a_media_source(self) -> None:
        export_day = date(2026, 8, 5)
        session = {"username": "wxid_degraded_image_placeholder", "displayName": "降级图片占位"}
        message = _message_for(export_day, 1)
        message.update({"localType": 3, "content": "[图片]"})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "bulk-raw"
            state_path = root / ".bulk-state.json"
            first = WeflowApiBackend(_config(root, media_localize=True))
            first._client = _BulkClient([session], {session["username"]: [message]})
            first.export_chats_range(
                export_day,
                export_day,
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
            )
            resumed_client = _BulkClient([session], {session["username"]: [message]})
            resumed = WeflowApiBackend(_config(root, media_localize=True))
            resumed._client = resumed_client
            result = resumed.export_chats_range(
                export_day,
                export_day,
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
            )

        self.assertEqual(resumed_client.message_calls, [])
        self.assertEqual(result.resumed_intervals, 1)

    def test_resume_state_rejects_an_existing_media_reference_escaping_output_root(self) -> None:
        export_day = date(2026, 8, 5)
        session = {"username": "wxid_escape_placeholder", "displayName": "越界引用占位"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "segment" / "output"
            source = root / "image.jpg"
            source.write_bytes(b"escape-reference-source")
            message = _message_for(export_day, 1)
            message.update(
                {
                    "localType": 3,
                    "content": "[图片]",
                    "mediaLocalPath": str(source),
                    "mediaFileName": source.name,
                    "mediaType": "image",
                }
            )
            state_path = root / ".bulk-state.json"
            backend = WeflowApiBackend(_config(root))
            backend._client = _BulkClient([session], {session["username"]: [message]})
            backend.export_chats_range(
                export_day,
                export_day,
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
            )
            session_dir = next(path for path in raw_root.iterdir() if path.is_dir() and path.name != "_media")
            json_path = next(session_dir.glob("*.json"))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            payload["messages"][0]["source"] = "../../../outside.bin"
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            (root / "outside.bin").write_bytes(b"must-still-be-illegal")
            reloaded = BulkExportState.load(state_path, raw_root)
            pending = reloaded.uncovered(session["username"], export_day, export_day, force=False)

        self.assertEqual(pending, [DateInterval(export_day, export_day)])

    def test_extending_range_fetches_only_uncovered_tail(self) -> None:
        first = date(2026, 8, 1)
        initial_end = date(2026, 8, 3)
        extended_end = date(2026, 8, 5)
        session = {"username": "wxid_extension_placeholder", "displayName": "扩展会话占位"}
        messages = [
            _message_for(date(2026, 8, day), day)
            for day in range(1, 6)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            raw_root = root / "bulk-raw"
            state_path = root / ".bulk-state.json"
            first_client = _BulkClient([session], {session["username"]: messages})
            backend = WeflowApiBackend(cfg)
            backend._client = first_client
            backend.export_chats_range(
                first,
                initial_end,
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
            )

            extension_client = _BulkClient([session], {session["username"]: messages})
            backend = WeflowApiBackend(cfg)
            backend._client = extension_client
            backend.export_chats_range(
                first,
                extended_end,
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
                expand_range=True,
            )
            exported_days = sorted(path.parent.name[-8:] for path in raw_root.glob("*/*.json"))

        self.assertEqual(
            extension_client.message_calls,
            [
                (session["username"], date(2026, 8, 4), date(2026, 8, 5)),
                (session["username"], date(2026, 8, 4), date(2026, 8, 5)),
            ],
        )
        self.assertEqual(
            exported_days,
            ["20260801", "20260802", "20260803", "20260804", "20260805"],
        )

    def test_existing_plan_rejects_implicit_range_change_and_any_shrink(self) -> None:
        first, initial_end, extended_end = (
            date(2026, 8, 1),
            date(2026, 8, 3),
            date(2026, 8, 5),
        )
        session = {"username": "wxid_frozen_range", "displayName": "冻结区间会话"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "raw"
            state_path = root / ".state.json"
            backend = WeflowApiBackend(_config(root))
            backend._client = _BulkClient([session], {session["username"]: []})
            backend.export_chats_range(
                first,
                initial_end,
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
            )

            frozen_resume = WeflowApiBackend(_config(root))
            frozen_client = _BulkClient([session], {session["username"]: []})
            frozen_resume._client = frozen_client
            frozen_result = frozen_resume.export_chats_range(
                None,
                None,
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
            )

            rejected = WeflowApiBackend(_config(root))
            rejected_client = _BulkClient([session], {session["username"]: []})
            rejected._client = rejected_client
            with self.assertRaisesRegex(ValueError, "plan 区间是 .*本次请求"):
                rejected.export_chats_range(
                    first,
                    extended_end,
                    raw_root=raw_root,
                    sessions=[session],
                    state_path=state_path,
                )

            shrink = WeflowApiBackend(_config(root))
            shrink._client = _BulkClient([session], {session["username"]: []})
            with self.assertRaisesRegex(ValueError, "只能扩、不能缩"):
                shrink.export_chats_range(
                    date(2026, 8, 2),
                    extended_end,
                    raw_root=raw_root,
                    sessions=[session],
                    state_path=state_path,
                    expand_range=True,
                )

        self.assertEqual(rejected_client.message_calls, [])
        self.assertEqual(frozen_client.message_calls, [])
        self.assertEqual(frozen_result.resumed_plans, 1)

    def test_missing_dates_without_a_plan_fails_loudly(self) -> None:
        session = {"username": "wxid_first_run", "displayName": "首次运行会话"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = WeflowApiBackend(_config(root))
            backend._client = _BulkClient([session], {})
            with self.assertRaisesRegex(ValueError, "首次运行时显式提供 --start 与 --end"):
                backend.export_chats_range(
                    None,
                    None,
                    raw_root=root / "raw",
                    sessions=[session],
                    state_path=root / ".state.json",
                )

    def test_message_target_chunking_keeps_every_day_and_rejects_bad_partitions(self) -> None:
        first = date(2026, 8, 1)
        interval = DateInterval(first, date(2026, 8, 6))
        counts = {first + timedelta(days=offset): value for offset, value in enumerate([4, 0, 7, 13, 3, 8])}

        chunks = _chunk_interval_by_messages(interval, counts, target_messages=10)

        self.assertEqual(
            [(chunk.start, chunk.end) for chunk in chunks],
            [
                (date(2026, 8, 1), date(2026, 8, 2)),
                (date(2026, 8, 3), date(2026, 8, 3)),
                (date(2026, 8, 4), date(2026, 8, 4)),
                (date(2026, 8, 5), date(2026, 8, 5)),
                (date(2026, 8, 6), date(2026, 8, 6)),
            ],
        )
        _validate_interval_partition(interval, chunks)
        with self.assertRaisesRegex(ValueError, "缺口"):
            _validate_interval_partition(interval, [DateInterval(first, first)])
        with self.assertRaisesRegex(ValueError, "重叠"):
            _validate_interval_partition(
                interval,
                [DateInterval(first, date(2026, 8, 3)), DateInterval(date(2026, 8, 3), interval.end)],
            )
        with self.assertRaisesRegex(ValueError, "越出"):
            _validate_interval_partition(
                interval,
                [DateInterval(date(2026, 7, 31), interval.end)],
            )

    def test_plan_distinguishes_an_empty_session_from_a_failed_fetch(self) -> None:
        class OnePlanFailureClient(_BulkClient):
            def fetch_messages(self, talker, *, start, end, **kwargs):
                if talker == "wxid_plan_failure_placeholder":
                    raise RuntimeError("injected plan fetch failure")
                return super().fetch_messages(talker, start=start, end=end, **kwargs)

        first = date(2026, 8, 1)
        second = date(2026, 8, 2)
        empty = {"username": "wxid_empty_placeholder", "displayName": "空会话占位"}
        failed = {"username": "wxid_plan_failure_placeholder", "displayName": "失败会话占位"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "bulk-raw"
            state_path = root / ".bulk-state.json"
            backend = WeflowApiBackend(_config(root))
            backend._client = OnePlanFailureClient([empty, failed], {empty["username"]: []})
            result = backend.export_chats_range(
                first,
                second,
                raw_root=raw_root,
                sessions=[empty, failed],
                state_path=state_path,
            )
            plan = json.loads(state_path.with_name(f"{state_path.name}.plan.json").read_text(encoding="utf-8"))

        empty_plan = plan["sessions"][empty["username"]]
        failed_plan = plan["sessions"][failed["username"]]
        self.assertEqual(empty_plan["dailyCounts"], {})
        self.assertNotIn("lastFailure", empty_plan)
        self.assertIn("lastFailure", failed_plan)
        self.assertNotIn("dailyCounts", failed_plan)
        self.assertIn(failed["username"], result.failures)

    def test_real_v1_shapes_migrate_sparse_without_losing_zero_coverage(self) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "bulk_export_v1_real_shape.json").read_text(
                encoding="utf-8"
            )
        )
        first, second = date(2026, 8, 1), date(2026, 8, 2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "raw"
            canonical = raw_root / "中性活跃会话" / "2026-08-02.json"
            canonical.parent.mkdir(parents=True)
            canonical.write_text('{"messages": []}', encoding="utf-8")

            plan_payload = fixture["plan"]
            plan_payload["outputRoot"] = str(raw_root.resolve())
            plan_path = root / ".plan.json"
            plan_path.write_text(json.dumps(plan_payload, ensure_ascii=False), encoding="utf-8")
            plan = BulkExportPlan.load(plan_path, raw_root)
            self.assertEqual(
                plan.daily_counts("wxid_neutral_active", first, second),
                {first: 0, second: 3},
            )
            self.assertEqual(plan.recorded_range(), DateInterval(first, second))
            plan.save()
            migrated_plan = json.loads(plan_path.read_text(encoding="utf-8"))

            state_payload = fixture["state"]
            state_payload["outputRoot"] = str(raw_root.resolve())
            state_path = root / ".state.json"
            state_path.write_text(json.dumps(state_payload, ensure_ascii=False), encoding="utf-8")
            state = BulkExportState.load(state_path, raw_root)
            self.assertEqual(
                state.completed_daily_counts("wxid_neutral_active", first, second),
                {first: 0, second: 4},
            )
            state.save()
            migrated_state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(migrated_plan["version"], 2)
        self.assertEqual(
            migrated_plan["sessions"]["wxid_neutral_active"]["dailyCounts"],
            {second.isoformat(): 3},
        )
        self.assertEqual(migrated_state["version"], 2)
        self.assertEqual(
            migrated_state["sessions"]["wxid_neutral_active"]["completed"][0]["dailyCounts"],
            {second.isoformat(): 4},
        )
        self.assertEqual(fixture["reconciliation"]["sessions"][0]["status"], "drift_or_incomplete")

    def test_plan_with_a_coverage_gap_refuses_to_supply_denominator(self) -> None:
        first = date(2026, 8, 1)
        missing = date(2026, 8, 2)
        last = date(2026, 8, 3)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "raw"
            plan_path = root / ".plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "outputRoot": str(raw_root.resolve()),
                        "range": {"start": first.isoformat(), "end": last.isoformat()},
                        "sessions": {
                            "wxid_gap_placeholder": {
                                "displayName": "覆盖缺口会话",
                                "completed": [
                                    {"start": first.isoformat(), "end": first.isoformat()},
                                    {"start": last.isoformat(), "end": last.isoformat()},
                                ],
                                "dailyCounts": {first.isoformat(): 2, last.isoformat(): 1},
                            }
                        },
                        "noLocalRecords": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            plan = BulkExportPlan.load(plan_path, raw_root)

            # 阳性对照：若覆盖证据被错误地补成连续区间，判据必须先能变绿。
            self.assertEqual(plan.uncovered("wxid_gap_placeholder", first, last), [DateInterval(missing, missing)])
            self.assertIsNone(plan.daily_counts("wxid_gap_placeholder", first, last))

    def test_no_local_records_is_classified_once_skipped_then_explicitly_retried(self) -> None:
        class MissingDatabaseClient(_BulkClient):
            def fetch_messages(self, talker, *, start, end, **_kwargs):
                self.message_calls.append((talker, start, end))
                raise WeflowApiError(
                    "WeFlow API HTTP 500: /api/v1/messages — 创建游标失败: -3（消息数据库未找到）",
                    status=500,
                )

        export_day = date(2026, 8, 5)
        session = {
            "username": "wxid_no_local_placeholder",
            "displayName": "本机无记录会话",
            "lastTimestamp": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "raw"
            state_path = root / ".state.json"
            first_client = MissingDatabaseClient([session], {})
            first = WeflowApiBackend(_config(root))
            first._client = first_client
            first_result = first.export_chats_range(
                export_day,
                export_day,
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
            )

            skipped_client = MissingDatabaseClient([session], {})
            skipped = WeflowApiBackend(_config(root))
            skipped._client = skipped_client
            skipped_result = skipped.export_chats_range(
                export_day,
                export_day,
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
            )

            retry_client = _BulkClient([session], {session["username"]: []})
            retry = WeflowApiBackend(_config(root))
            retry._client = retry_client
            retry_result = retry.export_chats_range(
                export_day,
                export_day,
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
                retry_no_local_records=True,
            )
            plan = json.loads(
                state_path.with_name(f"{state_path.name}.plan.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(first_client.message_calls), 1)
        self.assertEqual(first_result.failures, {})
        self.assertEqual(first_result.no_local_records, 1)
        self.assertEqual(skipped_client.message_calls, [])
        self.assertEqual(skipped_result.no_local_records, 1)
        self.assertEqual(len(retry_client.message_calls), 2)
        self.assertEqual(retry_result.no_local_records, 0)
        self.assertNotIn(session["username"], plan["noLocalRecords"])

    def test_daily_no_local_state_seeds_bulk_plan_without_an_api_retry(self) -> None:
        export_day = date(2026, 8, 5)
        session = {
            "username": "wxid_known_empty_placeholder",
            "displayName": "已知空会话",
            "lastTimestamp": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".export-state.json").write_text(
                json.dumps(
                    {
                        "version": 3,
                        "failures": {},
                        "ignored": {},
                        "noLocalRecords": {
                            session["username"]: {
                                "wxid": session["username"],
                                "displayName": session["displayName"],
                                "firstDetectedDate": "2026-08-01",
                                "lastDetectedDate": "2026-08-04",
                                "lastTimestamp": None,
                                "lastError": "neutral verified empty-session evidence",
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            client = _BulkClient([session], {})
            backend = WeflowApiBackend(_config(root))
            backend._client = client
            result = backend.export_chats_range(
                export_day,
                export_day,
                raw_root=root / "raw",
                sessions=[session],
                state_path=root / ".state.json",
            )

        self.assertEqual(client.message_calls, [])
        self.assertEqual(result.failures, {})
        self.assertEqual(result.no_local_records, 1)

    def test_plan_is_media_free_and_uses_the_export_day_splitter(self) -> None:
        class CaptureMediaClient(_BulkClient):
            media_flags: list[bool] = []

            def fetch_messages(self, talker, *, start, end, **kwargs):
                self.media_flags.append(kwargs["media"])
                return super().fetch_messages(talker, start=start, end=end, **kwargs)

        first = date(2026, 8, 1)
        second = date(2026, 8, 2)
        session = {"username": "wxid_plan_media_placeholder", "displayName": "plan 媒体占位"}
        messages = [_message_for(first, 1), _message_for(second, 2)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / ".bulk-state.json"
            client = CaptureMediaClient([session], {session["username"]: messages})
            backend = WeflowApiBackend(_config(root, media_localize=True))
            backend._client = client
            backend.export_chats_range(
                first,
                second,
                raw_root=root / "bulk-raw",
                sessions=[session],
                state_path=state_path,
            )
            plan = json.loads(state_path.with_name(f"{state_path.name}.plan.json").read_text(encoding="utf-8"))

        self.assertEqual(client.media_flags, [False, True])
        self.assertEqual(
            plan["sessions"][session["username"]]["dailyCounts"],
            {first.isoformat(): 1, second.isoformat(): 1},
        )

    def test_replan_refreshes_only_the_plan_not_completed_exports(self) -> None:
        export_day = date(2026, 8, 5)
        session = {"username": "wxid_replan_placeholder", "displayName": "重算 plan 占位"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "bulk-raw"
            state_path = root / ".bulk-state.json"
            first = WeflowApiBackend(_config(root))
            first._client = _BulkClient([session], {session["username"]: [_message_for(export_day, 1)]})
            first.export_chats_range(
                export_day,
                export_day,
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
            )

            refreshed_client = _BulkClient(
                [session], {session["username"]: [_message_for(export_day, 1), _message_for(export_day, 2)]}
            )
            refreshed = WeflowApiBackend(_config(root))
            refreshed._client = refreshed_client
            result = refreshed.export_chats_range(
                export_day,
                export_day,
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
                replan=True,
            )
            plan = json.loads(state_path.with_name(f"{state_path.name}.plan.json").read_text(encoding="utf-8"))

        self.assertEqual(refreshed_client.message_calls, [(session["username"], export_day, export_day)])
        self.assertEqual(result.completed_intervals, 0)
        self.assertEqual(result.resumed_intervals, 1)
        self.assertEqual(plan["sessions"][session["username"]]["dailyCounts"], {export_day.isoformat(): 2})

    def test_interruption_resumes_from_the_last_completed_chunk(self) -> None:
        class ChunkInterruptingClient(_BulkClient):
            calls = 0

            def fetch_messages(self, talker, *, start, end, **kwargs):
                self.calls += 1
                # Call one is the all-days plan; call three is the second export block.
                if self.calls == 3:
                    raise KeyboardInterrupt("injected chunk interruption")
                return super().fetch_messages(talker, start=start, end=end, **kwargs)

        start = date(2026, 8, 1)
        end = date(2026, 8, 3)
        session = {"username": "wxid_chunk_resume_placeholder", "displayName": "切块续跑占位"}
        messages = [_message_for(start + timedelta(days=offset), offset + 1) for offset in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "bulk-raw"
            state_path = root / ".bulk-state.json"
            first = WeflowApiBackend(_config(root))
            first._client = ChunkInterruptingClient([session], {session["username"]: messages})
            with self.assertRaisesRegex(KeyboardInterrupt, "chunk interruption"):
                first.export_chats_range(
                    start,
                    end,
                    raw_root=raw_root,
                    sessions=[session],
                    state_path=state_path,
                    chunk_target=1,
                )

            resumed_client = _BulkClient([session], {session["username"]: messages})
            resumed = WeflowApiBackend(_config(root))
            resumed._client = resumed_client
            result = resumed.export_chats_range(
                start,
                end,
                raw_root=raw_root,
                sessions=[session],
                state_path=state_path,
                chunk_target=1,
            )

        self.assertEqual(
            resumed_client.message_calls,
            [
                (session["username"], date(2026, 8, 2), date(2026, 8, 2)),
                (session["username"], date(2026, 8, 3), date(2026, 8, 3)),
            ],
        )
        self.assertEqual(result.completed_intervals, 2)
        self.assertEqual(result.resumed_plans, 1)

    def test_reconciliation_reports_plan_export_drift_without_marking_export_failed(self) -> None:
        class DriftingClient(_BulkClient):
            calls = 0

            def fetch_messages(self, talker, *, start, end, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    return []
                return super().fetch_messages(talker, start=start, end=end, **kwargs)

        export_day = date(2026, 8, 5)
        session = {"username": "wxid_drift_placeholder", "displayName": "漂移会话占位"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = WeflowApiBackend(_config(root))
            backend._client = DriftingClient(
                [session], {session["username"]: [_message_for(export_day, 1)]}
            )
            result = backend.export_chats_range(
                export_day,
                export_day,
                raw_root=root / "bulk-raw",
                sessions=[session],
                state_path=root / ".bulk-state.json",
            )
            reconciliation = json.loads(result.reconciliation_path.read_text(encoding="utf-8"))

        self.assertEqual(result.failures, {})
        self.assertEqual(result.reconciliation_differences, 1)
        entry = reconciliation["sessions"][0]
        self.assertEqual(entry["status"], "drift_or_incomplete")
        self.assertEqual(entry["differences"][0]["plannedMessages"], 1)
        self.assertEqual(entry["differences"][0]["exportedMessages"], 0)

    def test_bulk_result_uses_current_contact_name_for_operator_output(self) -> None:
        class CurrentContactClient(_BulkClient):
            def fetch_contacts(self, *, limit):
                return [
                    {
                        "username": "wxid_current_name_placeholder",
                        "displayName": "名册当前名称",
                    }
                ]

        export_day = date(2026, 8, 5)
        session = {
            "username": "wxid_current_name_placeholder",
            "displayName": "会话陈旧快照",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = WeflowApiBackend(_config(root))
            backend._client = CurrentContactClient(
                [session], {session["username"]: [_message_for(export_day, 1)]}
            )
            result = backend.export_chats_range(
                export_day,
                export_day,
                raw_root=root / "bulk-raw",
                sessions=[session],
                state_path=root / ".bulk-state.json",
            )

        self.assertEqual(
            result.session_display_names,
            {"wxid_current_name_placeholder": "名册当前名称"},
        )

    def test_block_state_uses_a_journal_between_compactions(self) -> None:
        export_day = date(2026, 8, 5)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "raw"
            raw_root.mkdir()
            first_json = raw_root / "first.json"
            second_json = raw_root / "second.json"
            first_json.write_text('{"messages": []}', encoding="utf-8")
            second_json.write_text('{"messages": []}', encoding="utf-8")
            state_path = root / ".bulk-state.json"
            state = BulkExportState.load(state_path, raw_root)
            state.record_completed(
                "wxid_state_placeholder",
                "状态占位",
                DateInterval(export_day, export_day),
                [first_json],
                daily_counts={export_day: 1},
            )
            next_day = export_day + timedelta(days=1)
            state.record_completed(
                "wxid_state_placeholder",
                "状态占位",
                DateInterval(next_day, next_day),
                [second_json],
                daily_counts={next_day: 1},
            )
            reloaded = BulkExportState.load(state_path, raw_root)
            journal_exists = reloaded.journal_path.exists()
            uncovered = reloaded.uncovered("wxid_state_placeholder", export_day, next_day, force=False)

        self.assertTrue(journal_exists)
        self.assertEqual(uncovered, [])

    def test_bulk_failure_uses_own_state_and_never_writes_daily_failure_state(self) -> None:
        class FailingClient(_BulkClient):
            def fetch_messages(self, talker, *, start, end, **_kwargs):
                self.message_calls.append((talker, start, end))
                raise RuntimeError("injected bulk failure")

        session = {"username": "wxid_failure_placeholder", "displayName": "失败会话占位"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            state_path = root / ".bulk-state.json"
            backend = WeflowApiBackend(cfg)
            backend._client = FailingClient([session], {})

            result = backend.export_chats_range(
                date(2026, 8, 1),
                date(2026, 8, 2),
                raw_root=root / "bulk-raw",
                sessions=[session],
                state_path=state_path,
            )
            second_result = backend.export_chats_range(
                date(2026, 8, 1),
                date(2026, 8, 2),
                raw_root=root / "bulk-raw",
                sessions=[session],
                state_path=state_path,
            )
            self.assertIn(session["username"], result.failures)
            self.assertIn(session["username"], second_result.failures)
            self.assertEqual(len(backend.client.message_calls), 2)
            plan_path = state_path.with_name(f"{state_path.name}.plan.json")
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertIn("lastFailure", plan["sessions"][session["username"]])
            self.assertTrue(state_path.exists())
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["version"], 2)
            self.assertFalse((root / ".export-state.json").exists())

    def test_cli_session_queries_are_repeatable_and_deduplicated(self) -> None:
        sessions = [
            {"username": "wxid_first_placeholder", "displayName": "第一会话占位"},
            {"username": "wxid_second_placeholder", "displayName": "第二会话占位"},
        ]

        selected = export_bulk_raw.select_sessions(
            sessions,
            ["wxid_first_placeholder", "第一会话", "wxid_second_placeholder"],
        )

        self.assertEqual(
            [session["username"] for session in selected],
            ["wxid_first_placeholder", "wxid_second_placeholder"],
        )

    def test_cli_accepts_explicit_chunk_target_and_replan(self) -> None:
        args = export_bulk_raw.build_parser().parse_args(
            [
                "--start",
                "2026-08-01",
                "--end",
                "2026-08-02",
                "--out",
                "synthetic-output",
                "--chunk-target",
                "321",
                "--replan",
                "--expand-range",
                "--retry-no-local-records",
                "--log",
                "driver.log",
            ]
        )

        self.assertEqual(args.chunk_target, 321)
        self.assertTrue(args.replan)
        self.assertTrue(args.expand_range)
        self.assertTrue(args.retry_no_local_records)
        self.assertEqual(args.log, "driver.log")

    def test_cli_accepts_frozen_range_resume_without_dates(self) -> None:
        args = export_bulk_raw.build_parser().parse_args(["--out", "synthetic-output"])

        self.assertIsNone(args.start)
        self.assertIsNone(args.end)

    def test_cli_uses_bulk_config_for_first_plan(self) -> None:
        fake_backend = Mock()
        fake_backend.client.fetch_sessions.return_value = []
        fake_backend.export_chats_range.return_value = BulkExportResult(
            requested_sessions=0,
            completed_intervals=0,
            resumed_intervals=0,
            skipped_platform_accounts=0,
            messages_written=0,
            files_written=(),
            failures={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root, bulk_start="2021-02-03", bulk_end="2021-04-05")
            raw_root = root / "first-plan-raw"
            output = io.StringIO()
            with (
                patch.object(export_bulk_raw, "load_config", return_value=cfg),
                patch.object(export_bulk_raw, "resolve_config_path", return_value=root / "config.toml"),
                patch.object(export_bulk_raw, "WeflowApiBackend", return_value=fake_backend),
                redirect_stdout(output),
            ):
                code = export_bulk_raw.main(["--out", str(raw_root)])

            call = fake_backend.export_chats_range.call_args

        self.assertEqual(code, 0)
        self.assertEqual(call.args, (date(2021, 2, 3), date(2021, 4, 5)))
        self.assertIn("无 plan，使用 [bulk_export] 配置 2021-02-03~2021-04-05", output.getvalue())

    def test_cli_empty_config_end_rolls_existing_plan_to_yesterday_with_notice(self) -> None:
        yesterday = date.today() - timedelta(days=1)
        frozen_start, frozen_end = yesterday - timedelta(days=3), yesterday - timedelta(days=1)
        session = {"username": "wxid_range_placeholder", "displayName": "区间会话占位"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "frozen-plan-raw"
            plan_path = _seed_driver_plan(raw_root, frozen_start, frozen_end)
            cfg = _config(root, bulk_start=frozen_start.isoformat(), bulk_end="")
            backend = WeflowApiBackend(cfg)
            backend._client = _BulkClient([session], {session["username"]: []})
            output = io.StringIO()
            with (
                patch.object(export_bulk_raw, "load_config", return_value=cfg),
                patch.object(export_bulk_raw, "resolve_config_path", return_value=root / "config.toml"),
                patch.object(export_bulk_raw, "WeflowApiBackend", return_value=backend),
                redirect_stdout(output),
            ):
                code = export_bulk_raw.main(["--out", str(raw_root)])
            recorded = BulkExportPlan.load(plan_path, raw_root).recorded_range()

        self.assertEqual(code, 0)
        self.assertEqual(recorded, DateInterval(frozen_start, yesterday))
        self.assertIn(
            f"本次把区间从 {frozen_start}~{frozen_end} 扩到 {frozen_start}~{yesterday}，"
            "需为 1 个会话补规划",
            output.getvalue(),
        )

    def test_cli_empty_config_end_equal_to_yesterday_keeps_plan_without_notice(self) -> None:
        yesterday = date.today() - timedelta(days=1)
        frozen_start = yesterday - timedelta(days=2)
        session = {"username": "wxid_range_placeholder", "displayName": "区间会话占位"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "same-plan-raw"
            plan_path = _seed_driver_plan(raw_root, frozen_start, yesterday)
            cfg = _config(root, bulk_start=frozen_start.isoformat(), bulk_end="")
            backend = WeflowApiBackend(cfg)
            backend._client = _BulkClient([session], {session["username"]: []})
            output = io.StringIO()
            with (
                patch.object(export_bulk_raw, "load_config", return_value=cfg),
                patch.object(export_bulk_raw, "resolve_config_path", return_value=root / "config.toml"),
                patch.object(export_bulk_raw, "WeflowApiBackend", return_value=backend),
                redirect_stdout(output),
            ):
                code = export_bulk_raw.main(["--out", str(raw_root)])
            recorded = BulkExportPlan.load(plan_path, raw_root).recorded_range()

        self.assertEqual(code, 0)
        self.assertEqual(recorded, DateInterval(frozen_start, yesterday))
        self.assertNotIn("本次把区间从", output.getvalue())

    def test_cli_empty_config_end_keeps_future_plan_without_error_or_notice(self) -> None:
        yesterday = date.today() - timedelta(days=1)
        frozen_start, frozen_end = yesterday - timedelta(days=2), yesterday + timedelta(days=2)
        session = {"username": "wxid_range_placeholder", "displayName": "区间会话占位"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "future-plan-raw"
            plan_path = _seed_driver_plan(raw_root, frozen_start, frozen_end)
            cfg = _config(root, bulk_start=frozen_start.isoformat(), bulk_end="")
            backend = WeflowApiBackend(cfg)
            backend._client = _BulkClient([session], {session["username"]: []})
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch.object(export_bulk_raw, "load_config", return_value=cfg),
                patch.object(export_bulk_raw, "resolve_config_path", return_value=root / "config.toml"),
                patch.object(export_bulk_raw, "WeflowApiBackend", return_value=backend),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                code = export_bulk_raw.main(["--out", str(raw_root)])
            recorded = BulkExportPlan.load(plan_path, raw_root).recorded_range()

        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(recorded, DateInterval(frozen_start, frozen_end))
        self.assertNotIn("本次把区间从", output.getvalue())

    def test_cli_explicit_earlier_config_start_expands_plan_with_notice(self) -> None:
        frozen_end = date.today() - timedelta(days=1)
        frozen_start, configured_start = frozen_end - timedelta(days=2), frozen_end - timedelta(days=4)
        session = {"username": "wxid_range_placeholder", "displayName": "区间会话占位"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "earlier-start-raw"
            plan_path = _seed_driver_plan(raw_root, frozen_start, frozen_end)
            cfg = _config(
                root,
                bulk_start=configured_start.isoformat(),
                bulk_end=frozen_end.isoformat(),
            )
            backend = WeflowApiBackend(cfg)
            backend._client = _BulkClient([session], {session["username"]: []})
            output = io.StringIO()
            with (
                patch.object(export_bulk_raw, "load_config", return_value=cfg),
                patch.object(export_bulk_raw, "resolve_config_path", return_value=root / "config.toml"),
                patch.object(export_bulk_raw, "WeflowApiBackend", return_value=backend),
                redirect_stdout(output),
            ):
                code = export_bulk_raw.main(["--out", str(raw_root)])
            recorded = BulkExportPlan.load(plan_path, raw_root).recorded_range()

        self.assertEqual(code, 0)
        self.assertEqual(recorded, DateInterval(configured_start, frozen_end))
        self.assertIn(
            f"本次把区间从 {frozen_start}~{frozen_end} 扩到 "
            f"{configured_start}~{frozen_end}，需为 1 个会话补规划",
            output.getvalue(),
        )

    def test_cli_rejects_explicit_config_end_before_plan_end(self) -> None:
        frozen_start, frozen_end = date(2026, 7, 2), date(2026, 7, 9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "end-shrink-raw"
            _seed_driver_plan(raw_root, frozen_start, frozen_end)
            cfg = _config(
                root,
                bulk_start=frozen_start.isoformat(),
                bulk_end=date(2026, 7, 8).isoformat(),
            )
            errors = io.StringIO()
            with (
                patch.object(export_bulk_raw, "load_config", return_value=cfg),
                patch.object(export_bulk_raw, "resolve_config_path", return_value=root / "config.toml"),
                patch.object(export_bulk_raw, "WeflowApiBackend") as backend_class,
                redirect_stderr(errors),
            ):
                code = export_bulk_raw.main(["--out", str(raw_root)])

        self.assertEqual(code, 1)
        self.assertIn(f"plan 区间是 {frozen_start}~{frozen_end}", errors.getvalue())
        self.assertIn("配置只能扩、不能缩", errors.getvalue())
        backend_class.assert_not_called()

    def test_cli_rejects_explicit_config_start_after_plan_start(self) -> None:
        frozen_start, frozen_end = date(2026, 7, 2), date(2026, 7, 9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "start-shrink-raw"
            _seed_driver_plan(raw_root, frozen_start, frozen_end)
            cfg = _config(
                root,
                bulk_start=date(2026, 7, 3).isoformat(),
                bulk_end=frozen_end.isoformat(),
            )
            errors = io.StringIO()
            with (
                patch.object(export_bulk_raw, "load_config", return_value=cfg),
                patch.object(export_bulk_raw, "resolve_config_path", return_value=root / "config.toml"),
                patch.object(export_bulk_raw, "WeflowApiBackend") as backend_class,
                redirect_stderr(errors),
            ):
                code = export_bulk_raw.main(["--out", str(raw_root)])

        self.assertEqual(code, 1)
        self.assertIn(f"plan 区间是 {frozen_start}~{frozen_end}", errors.getvalue())
        self.assertIn("配置只能扩、不能缩", errors.getvalue())
        backend_class.assert_not_called()

    def test_cli_explicit_range_still_rejects_plan_change_without_expand_flag(self) -> None:
        frozen_start, frozen_end = date(2026, 7, 2), date(2026, 7, 9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "cli-guard-raw"
            _seed_driver_plan(raw_root, frozen_start, frozen_end)
            cfg = _config(root, bulk_start="invalid-but-unused", bulk_end="also-unused")
            errors = io.StringIO()
            with (
                patch.object(export_bulk_raw, "load_config", return_value=cfg),
                patch.object(export_bulk_raw, "resolve_config_path", return_value=root / "config.toml"),
                patch.object(export_bulk_raw, "WeflowApiBackend") as backend_class,
                redirect_stderr(errors),
            ):
                code = export_bulk_raw.main(
                    [
                        "--start",
                        frozen_start.isoformat(),
                        "--end",
                        date(2026, 7, 10).isoformat(),
                        "--out",
                        str(raw_root),
                    ]
                )

        self.assertEqual(code, 1)
        self.assertIn("默认拒绝改动", errors.getvalue())
        self.assertNotIn("[bulk_export]", errors.getvalue())
        backend_class.assert_not_called()

    def test_cli_rejects_invalid_bulk_config_in_chinese(self) -> None:
        fake_backend = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root, bulk_start="not-a-date")
            errors = io.StringIO()
            with (
                patch.object(export_bulk_raw, "load_config", return_value=cfg),
                patch.object(export_bulk_raw, "resolve_config_path", return_value=root / "config.toml"),
                patch.object(export_bulk_raw, "WeflowApiBackend", return_value=fake_backend),
                redirect_stderr(errors),
            ):
                code = export_bulk_raw.main(["--out", str(root / "raw")])

        self.assertEqual(code, 1)
        self.assertIn("[bulk_export].start 配置无效", errors.getvalue())
        fake_backend.prepare_bulk_export.assert_not_called()

    def test_print_range_without_plan_emits_parseable_config_json_and_writes_nothing(self) -> None:
        yesterday = date.today() - timedelta(days=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root, bulk_start="", bulk_end="")
            raw_root = root / "print-only-raw"
            log_path = root / "must-not-be-created.log"
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch.object(export_bulk_raw, "WeflowApiBackend") as backend_class,
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                code = export_bulk_raw.main(
                    [
                        "--print-range",
                        "--out",
                        str(raw_root),
                        "--config",
                        str(root / "config.toml"),
                        "--log",
                        str(log_path),
                    ]
                )
            payload = json.loads(output.getvalue())
            wrote_raw_root = raw_root.exists()
            wrote_plan = export_bulk_raw.bulk_plan_path_for(raw_root).exists()
            wrote_state = export_bulk_raw.bulk_state_path_for(raw_root).exists()
            wrote_log = log_path.exists()

        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(
            payload,
            {
                "start": "2010-01-01",
                "end": yesterday.isoformat(),
                "source": "config",
                "planRange": None,
                "expands": False,
            },
        )
        self.assertEqual(
            output.getvalue(),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        )
        self.assertFalse(wrote_raw_root)
        self.assertFalse(wrote_plan)
        self.assertFalse(wrote_state)
        self.assertFalse(wrote_log)
        backend_class.assert_not_called()

    def test_print_range_with_unchanged_plan_reports_plan_source(self) -> None:
        yesterday = date.today() - timedelta(days=1)
        frozen_start = yesterday - timedelta(days=2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "print-plan-raw"
            plan_path = _seed_driver_plan(raw_root, frozen_start, yesterday)
            original_plan = plan_path.read_bytes()
            _config(root, bulk_start=frozen_start.isoformat(), bulk_end="")
            output = io.StringIO()
            with (
                patch.object(export_bulk_raw, "WeflowApiBackend") as backend_class,
                redirect_stdout(output),
            ):
                code = export_bulk_raw.main(
                    [
                        "--print-range",
                        "--out",
                        str(raw_root),
                        "--config",
                        str(root / "config.toml"),
                    ]
                )
            payload = json.loads(output.getvalue())
            after_plan = plan_path.read_bytes()

        self.assertEqual(code, 0)
        self.assertEqual(payload["source"], "plan")
        self.assertFalse(payload["expands"])
        self.assertEqual(
            payload["planRange"],
            {"start": frozen_start.isoformat(), "end": yesterday.isoformat()},
        )
        self.assertEqual(after_plan, original_plan)
        backend_class.assert_not_called()

    def test_print_range_with_rolling_config_expansion_reports_plan_plus_config(self) -> None:
        yesterday = date.today() - timedelta(days=1)
        frozen_start, frozen_end = yesterday - timedelta(days=3), yesterday - timedelta(days=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "print-expanded-raw"
            plan_path = _seed_driver_plan(raw_root, frozen_start, frozen_end)
            original_plan = plan_path.read_bytes()
            _config(root, bulk_start=frozen_start.isoformat(), bulk_end="")
            output = io.StringIO()
            with (
                patch.object(export_bulk_raw, "WeflowApiBackend") as backend_class,
                redirect_stdout(output),
            ):
                code = export_bulk_raw.main(
                    [
                        "--print-range",
                        "--out",
                        str(raw_root),
                        "--config",
                        str(root / "config.toml"),
                    ]
                )
            payload = json.loads(output.getvalue())
            after_plan = plan_path.read_bytes()

        self.assertEqual(code, 0)
        self.assertEqual(payload["end"], yesterday.isoformat())
        self.assertEqual(payload["source"], "plan+config")
        self.assertTrue(payload["expands"])
        self.assertEqual(
            payload["planRange"],
            {"start": frozen_start.isoformat(), "end": frozen_end.isoformat()},
        )
        self.assertEqual(after_plan, original_plan)
        backend_class.assert_not_called()

    def test_print_range_config_shrink_is_nonzero_with_empty_stdout(self) -> None:
        frozen_start, frozen_end = date(2026, 7, 2), date(2026, 7, 9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "print-rejected-raw"
            _seed_driver_plan(raw_root, frozen_start, frozen_end)
            _config(root, bulk_start=frozen_start.isoformat(), bulk_end="2026-07-08")
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = export_bulk_raw.main(
                    [
                        "--print-range",
                        "--out",
                        str(raw_root),
                        "--config",
                        str(root / "config.toml"),
                    ]
                )

        self.assertNotEqual(code, 0)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("配置只能扩、不能缩", errors.getvalue())

    def test_print_range_cli_source_ignores_invalid_bulk_config_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "print-cli-raw"
            _config(root, bulk_start="invalid-but-unused", bulk_end="also-unused")
            output = io.StringIO()
            with (
                patch.object(export_bulk_raw, "WeflowApiBackend") as backend_class,
                redirect_stdout(output),
            ):
                code = export_bulk_raw.main(
                    [
                        "--print-range",
                        "--start",
                        "20260702",
                        "--end",
                        "2026-07-09",
                        "--out",
                        str(raw_root),
                        "--config",
                        str(root / "config.toml"),
                    ]
                )
            payload = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["source"], "cli")
        self.assertEqual(payload["start"], "2026-07-02")
        self.assertEqual(payload["end"], "2026-07-09")
        self.assertIsNone(payload["planRange"])
        self.assertFalse(payload["expands"])
        backend_class.assert_not_called()

    def test_print_range_corrupt_plan_is_nonzero_with_empty_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "print-corrupt-plan-raw"
            plan_path = export_bulk_raw.bulk_plan_path_for(raw_root)
            plan_path.write_text("not-json", encoding="utf-8")
            _config(root, bulk_start="2026-07-02", bulk_end="2026-07-09")
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = export_bulk_raw.main(
                    [
                        "--print-range",
                        "--out",
                        str(raw_root),
                        "--config",
                        str(root / "config.toml"),
                    ]
                )

        self.assertNotEqual(code, 0)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("plan 文件不可读", errors.getvalue())

    def test_driver_log_appends_timestamped_lines_without_changing_console(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "driver.log"
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                with export_bulk_raw._driver_log(str(log_path)):
                    print("stdout 原文")
                    print("stderr 原文", file=sys.stderr)
                with export_bulk_raw._driver_log(str(log_path)):
                    print("第二轮")
            lines = log_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(output.getvalue(), "stdout 原文\n第二轮\n")
        self.assertEqual(errors.getvalue(), "stderr 原文\n")
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(line.startswith("[") and line[3] == ":" and line[6] == ":" for line in lines))
        self.assertTrue(lines[0].endswith("stdout 原文"))
        self.assertTrue(lines[1].endswith("stderr 原文"))
        self.assertTrue(lines[2].endswith("第二轮"))

    def test_driver_log_also_captures_argument_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "driver.log"
            errors = io.StringIO()
            with redirect_stderr(errors), self.assertRaises(SystemExit):
                export_bulk_raw.main(
                    [
                        "--start",
                        "2026-08-01",
                        "--out",
                        "neutral-output",
                        "--log",
                        str(log_path),
                    ]
                )
            log_text = log_path.read_text(encoding="utf-8")

        self.assertIn("--start 与 --end 必须同时提供或同时省略", errors.getvalue())
        self.assertIn("--start 与 --end 必须同时提供或同时省略", log_text)
        self.assertTrue(all(line.startswith("[") for line in log_text.splitlines()))

    def test_failure_list_groups_display_names_by_normalized_fingerprint(self) -> None:
        failures = {
            "wxid_neutral_a": "HTTP 500 wxid_neutral_a request 41 failed",
            "wxid_neutral_b": "HTTP 503 wxid_neutral_b request 99 failed",
        }
        session_display_names = {
            "wxid_neutral_a": "名册当前名称甲",
            "wxid_neutral_b": "名册当前名称乙",
        }
        errors = io.StringIO()
        with redirect_stderr(errors):
            export_bulk_raw._print_failure_groups(failures, session_display_names)

        text = errors.getvalue()
        self.assertIn("失败清单（按指纹分组）", text)
        self.assertIn("指纹（2 个）", text)
        self.assertIn("名册当前名称甲", text)
        self.assertIn("名册当前名称乙", text)

    def test_cli_passes_explicit_range_selection_output_and_resume_state_to_backend(self) -> None:
        sessions = [
            {"username": "wxid_first_placeholder", "displayName": "第一会话占位"},
            {"username": "wxid_second_placeholder", "displayName": "第二会话占位"},
        ]
        fake_backend = Mock()
        fake_backend.client.fetch_sessions.return_value = sessions
        fake_backend.export_chats_range.return_value = BulkExportResult(
            requested_sessions=1,
            completed_intervals=1,
            resumed_intervals=0,
            skipped_platform_accounts=0,
            messages_written=1,
            files_written=(),
            failures={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            raw_root = root / "explicit-raw"
            output = io.StringIO()
            with (
                patch.object(export_bulk_raw, "load_config", return_value=cfg),
                patch.object(export_bulk_raw, "resolve_config_path", return_value=root / "config.toml"),
                patch.object(export_bulk_raw, "WeflowApiBackend", return_value=fake_backend),
                redirect_stdout(output),
            ):
                code = export_bulk_raw.main(
                    [
                        "--start",
                        "20260801",
                        "--end",
                        "2026-08-05",
                        "--out",
                        str(raw_root),
                        "--session",
                        "wxid_second_placeholder",
                    ]
                )

            call = fake_backend.export_chats_range.call_args

        self.assertEqual(code, 0)
        self.assertEqual(call.args, (date(2026, 8, 1), date(2026, 8, 5)))
        self.assertEqual(call.kwargs["raw_root"], raw_root.resolve())
        self.assertEqual(
            [session["username"] for session in call.kwargs["sessions"]],
            ["wxid_second_placeholder"],
        )
        self.assertEqual(
            call.kwargs["state_path"],
            raw_root.parent / f".{raw_root.name}.bulk-export-state.json",
        )
        self.assertEqual(
            call.kwargs["plan_path"],
            raw_root.parent / f".{raw_root.name}.bulk-export-plan.json",
        )
        self.assertEqual(call.kwargs["chunk_target"], 10_000)
        self.assertFalse(call.kwargs["replan"])
        self.assertFalse(call.kwargs["expand_range"])
        self.assertFalse(call.kwargs["retry_no_local_records"])
        fake_backend.prepare_bulk_export.assert_called_once_with()
        fake_backend.shutdown.assert_called_once_with()

    def test_cli_guard_stop_prints_reconciliation_and_summary_before_exit_one(self) -> None:
        session = {"username": "synthetic-health@chatroom", "displayName": "中性健康群"}
        fake_backend = Mock()
        fake_backend.client.fetch_sessions.return_value = [session]
        fake_backend.export_chats_range.return_value = BulkExportResult(
            requested_sessions=1,
            completed_intervals=1,
            resumed_intervals=0,
            skipped_platform_accounts=0,
            messages_written=1,
            files_written=(),
            failures={},
            reconciliation_path=Path("neutral-reconciliation.json"),
            stopped_reason="群成员名册服务疑似进入坏状态；请重启后续跑",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch.object(export_bulk_raw, "load_config", return_value=cfg),
                patch.object(export_bulk_raw, "resolve_config_path", return_value=root / "config.toml"),
                patch.object(export_bulk_raw, "WeflowApiBackend", return_value=fake_backend),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                code = export_bulk_raw.main(
                    [
                        "--start",
                        "2026-08-01",
                        "--end",
                        "2026-08-01",
                        "--out",
                        str(root / "raw"),
                    ]
                )

        self.assertEqual(code, 1)
        self.assertIn("对账：neutral-reconciliation.json", output.getvalue())
        self.assertIn("汇总：", output.getvalue())
        self.assertIn("批量 raw 导出停止", errors.getvalue())

    def test_cli_incomplete_coverage_is_visible_and_exits_one(self) -> None:
        session = {"username": "wxid_coverage_placeholder", "displayName": "覆盖会话占位"}
        start = date(2024, 1, 1)
        end = date(2024, 1, 5)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "raw"
            plan = BulkExportPlan.load(root / ".plan.json", raw_root)
            plan.resolve_range(start, end, allow_expand=False)
            plan.record_completed(
                session["username"],
                session["displayName"],
                DateInterval(start, end),
                {},
            )
            state = BulkExportState.load(root / ".state.json", raw_root)
            state.record_completed(
                session["username"],
                session["displayName"],
                DateInterval(start, date(2024, 1, 2)),
                [],
                daily_counts={},
            )
            reconciliation_path, differences, incomplete_days = _write_bulk_reconciliation(
                raw_root,
                plan,
                state,
                [session],
                contacts=[session],
                start=start,
                end=end,
                skip_platform_accounts=True,
            )
            fake_backend = Mock()
            fake_backend.client.fetch_sessions.return_value = [session]
            fake_backend.export_chats_range.return_value = BulkExportResult(
                requested_sessions=1,
                completed_intervals=1,
                resumed_intervals=0,
                skipped_platform_accounts=0,
                messages_written=0,
                files_written=(),
                failures={},
                reconciliation_path=reconciliation_path,
                reconciliation_differences=differences,
                reconciliation_incomplete_coverage_days=incomplete_days,
                session_display_names={session["username"]: session["displayName"]},
            )
            cfg = _config(root)
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch.object(export_bulk_raw, "load_config", return_value=cfg),
                patch.object(export_bulk_raw, "resolve_config_path", return_value=root / "config.toml"),
                patch.object(export_bulk_raw, "WeflowApiBackend", return_value=fake_backend),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                code = export_bulk_raw.main(
                    [
                        "--start",
                        start.isoformat(),
                        "--end",
                        end.isoformat(),
                        "--out",
                        str(raw_root),
                    ]
                )

        self.assertEqual(differences, 0)
        self.assertEqual(incomplete_days, 3)
        self.assertEqual(code, 1)
        self.assertIn("差异日 0；覆盖缺口 3", output.getvalue())
        self.assertIn("覆盖不完整", errors.getvalue())


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


if __name__ == "__main__":
    unittest.main()
