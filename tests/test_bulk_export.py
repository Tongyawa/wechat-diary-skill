from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch

from scripts import export_bulk_raw
from wechat_diary_core.backends.weflow_api.backend import BulkExportResult, WeflowApiBackend
from wechat_diary_core.backends.weflow_api.client import WeflowApiClient
from wechat_diary_core.config import load_config
from wechat_diary_core.raw_schema import validate_session_json


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "weflow_api"


def _config(root: Path, *, target_usernames: tuple[str, ...] = ()):
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
media_localize = false

[daily_export]
target_usernames = {json.dumps(list(target_usernames), ensure_ascii=False)}
skip_official_accounts = true

[asr]
engine = ""
""".strip(),
        encoding="utf-8",
    )
    return load_config(path)


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

    def fetch_messages(self, talker, *, start, end, **_kwargs):
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
        self.assertEqual([query["offset"] for query in queries], [["0"], ["2"]])
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
            def fetch_messages(self, talker, *, start, end, **kwargs):
                if talker == "wxid_second_placeholder":
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
            )
            exported_days = sorted(path.parent.name[-8:] for path in raw_root.glob("*/*.json"))

        self.assertEqual(
            extension_client.message_calls,
            [(session["username"], date(2026, 8, 4), date(2026, 8, 5))],
        )
        self.assertEqual(
            exported_days,
            ["20260801", "20260802", "20260803", "20260804", "20260805"],
        )

    def test_bulk_failure_uses_own_state_and_never_writes_daily_failure_state(self) -> None:
        class FailingClient(_BulkClient):
            def fetch_messages(self, talker, *, start, end, **_kwargs):
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
            self.assertIn(session["username"], result.failures)
            self.assertTrue(state_path.exists())
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
        fake_backend.prepare_bulk_export.assert_called_once_with()
        fake_backend.shutdown.assert_called_once_with()


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


if __name__ == "__main__":
    unittest.main()
