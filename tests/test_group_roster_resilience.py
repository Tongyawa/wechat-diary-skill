from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch

from wechat_diary_core.backends.weflow_api.backend import (
    BulkExportResult,
    WeflowApiBackend,
    _last_known_group_members,
)
from wechat_diary_core.config import load_config
from wechat_diary_core.raw_schema import validate_session_json


def _config(root: Path, *, media_localize: bool = True):
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
access_token = "synthetic-token"
media_localize = {str(media_localize).lower()}

[daily_export]
skip_official_accounts = true

[asr]
engine = ""
""".strip(),
        encoding="utf-8",
    )
    return load_config(path)


def _message(day: date, local_id: int, sender: str) -> dict:
    timestamp = int(datetime.combine(day, time(hour=12)).timestamp()) + local_id
    return {
        "localId": local_id,
        "serverId": f"synthetic-server-{local_id}",
        "localType": 1,
        "createTime": timestamp,
        "isSend": 0,
        "senderUsername": sender,
        "content": f"synthetic message {local_id}",
        "rawContent": "",
        "parsedContent": "",
        "replyToMessageId": "",
        "quote": None,
    }


class _RosterClient:
    def __init__(
        self,
        sessions: list[dict],
        messages: dict[str, list[dict]],
        *,
        failed_rosters: set[str] | None = None,
        extra_contacts: list[dict] | None = None,
    ) -> None:
        self.sessions = sessions
        self.messages = messages
        self.failed_rosters = failed_rosters or set()
        self.extra_contacts = extra_contacts or []
        self.group_member_calls: list[str] = []
        self.message_calls: list[tuple[str, bool]] = []

    def fetch_sessions(self, *, limit):
        return self.sessions

    def fetch_contacts(self, *, limit):
        return [
            *[
                {
                    "username": session["username"],
                    "displayName": session["displayName"],
                    "nickname": session["displayName"],
                }
                for session in self.sessions
            ],
            *self.extra_contacts,
        ]

    def fetch_messages(self, talker, *, start, end, media, **_kwargs):
        self.message_calls.append((talker, media))
        return [
            message
            for message in self.messages.get(talker, [])
            if start <= datetime.fromtimestamp(int(message["createTime"])).date() <= end
        ]

    def fetch_group_members(self, talker):
        self.group_member_calls.append(talker)
        if talker in self.failed_rosters:
            raise RuntimeError("synthetic group roster timeout")
        return [
            {
                "wxid": "wxid_member_placeholder",
                "displayName": "Synthetic Member",
                "nickname": "Synthetic Member",
            }
        ]


class GroupRosterResilienceTests(unittest.TestCase):
    def test_daily_roster_failure_preserves_every_message_and_enumerates_fallback(self) -> None:
        export_day = date(2026, 8, 25)
        talker = "synthetic-daily@chatroom"
        prior_sender = "wxid_prior_sender_placeholder"
        known_sender = "wxid_contact_sender_placeholder"
        unknown_sender = "wxid_unknown_sender_placeholder"
        sessions = [{"username": talker, "displayName": "Synthetic Daily Group"}]
        expected_ids = [101, 102, 103]
        messages = {
            talker: [
                _message(export_day, expected_ids[0], prior_sender),
                _message(export_day, expected_ids[1], known_sender),
                _message(export_day, expected_ids[2], unknown_sender),
            ]
        }

        def assert_complete(actual_messages: list[dict]) -> None:
            self.assertEqual([item["localId"] for item in actual_messages], expected_ids)

        # 阳性对照：旧行为等价于名册异常后发布空集合；完整性判据必须先打红。
        with self.assertRaises(AssertionError):
            assert_complete([])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            archived_session = cfg.paths.archived / "raw" / "群聊_Synthetic Daily Group"
            archived_session.mkdir(parents=True)
            (archived_session / "20260824.json").write_text(
                json.dumps(
                    {
                        "session": {"wxid": talker},
                        "messages": [
                            {
                                "createTime": 1,
                                "senderUsername": prior_sender,
                                "senderDisplayName": "Synthetic Last Known Member",
                                "senderNickname": "Synthetic Last Known Member",
                                "senderRemark": "",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            # 同名目录里的异群证据必须被忽略，避免把看似可信的错名带进产物。
            (archived_session / "wrong-group.json").write_text(
                json.dumps(
                    {
                        "session": {"wxid": "synthetic-other@chatroom"},
                        "messages": [
                            {
                                "createTime": 2,
                                "senderUsername": unknown_sender,
                                "senderDisplayName": "Must Not Leak Across Groups",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (archived_session / "broken.json").write_text("{not-json", encoding="utf-8")
            client = _RosterClient(
                sessions,
                messages,
                failed_rosters={talker},
                extra_contacts=[
                    {
                        "username": known_sender,
                        "displayName": "Synthetic Contact Fallback",
                        "nickname": "Synthetic Contact Fallback",
                    }
                ],
            )
            backend = WeflowApiBackend(cfg)
            backend._client = client
            errors = io.StringIO()
            with redirect_stderr(errors):
                backend.export_chats(export_day)
            export = next(cfg.paths.raw.glob("群聊_Synthetic Daily Group_20260825/*.json"))
            payload = json.loads(export.read_text(encoding="utf-8"))

        assert_complete(payload["messages"])
        self.assertEqual(
            [item["senderDisplayName"] for item in payload["messages"]],
            ["Synthetic Last Known Member", "Synthetic Contact Fallback", unknown_sender],
        )
        self.assertEqual(client.group_member_calls, [talker])
        self.assertEqual(
            backend.partial_failures,
            [f"export_chat_group_roster:{talker}:fallback=canonical:known_names=1"],
        )
        self.assertIn("群成员名册读取失败", errors.getvalue())
        self.assertIn("群成员名册降级汇总：1/1", errors.getvalue())
        self.assertTrue(all(len(line) < 240 for line in errors.getvalue().splitlines()))
        validate_session_json(payload)

    def test_unreadable_canonical_fallback_and_empty_api_roster_still_publish(self) -> None:
        export_day = date(2026, 8, 25)
        talker = "synthetic-empty-roster@chatroom"
        sessions = [{"username": talker, "displayName": "Synthetic Empty Roster Group"}]
        messages = {talker: [_message(export_day, 201, "wxid_unknown_placeholder")]}

        class EmptyRosterClient(_RosterClient):
            def fetch_group_members(self, current_talker):
                self.group_member_calls.append(current_talker)
                return []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(Path, "iterdir", side_effect=PermissionError("synthetic denied")):
                recovered = _last_known_group_members(
                    talker,
                    "Synthetic Empty Roster Group",
                    roots=(root,),
                )
            self.assertEqual(recovered, [])

            cfg = _config(root)
            backend = WeflowApiBackend(cfg)
            backend._client = EmptyRosterClient(sessions, messages)
            with redirect_stderr(io.StringIO()):
                backend.export_chats(export_day)
            export = next(cfg.paths.raw.glob("群聊_Synthetic Empty Roster Group_20260825/*.json"))
            payload = json.loads(export.read_text(encoding="utf-8"))

        self.assertEqual(payload["session"]["messageCount"], 1)
        self.assertEqual(
            backend.partial_failures,
            [f"export_chat_group_roster:{talker}:fallback=contacts:known_names=0"],
        )

    def test_bulk_roster_failure_preserves_all_later_chunks(self) -> None:
        days = [date(2026, 8, day) for day in (23, 24, 25)]
        talker = "synthetic-bulk@chatroom"
        sessions = [{"username": talker, "displayName": "Synthetic Bulk Group"}]
        messages = {
            talker: [
                _message(export_day, index, "wxid_member_placeholder")
                for index, export_day in enumerate(days, start=1)
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = _RosterClient(sessions, messages, failed_rosters={talker})
            backend = WeflowApiBackend(_config(root))
            backend._client = client
            result = backend.export_chats_range(
                days[0],
                days[-1],
                raw_root=root / "bulk-raw",
                sessions=sessions,
                state_path=root / ".bulk-state.json",
                chunk_target=1,
            )
            payloads = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted((root / "bulk-raw").glob("*/*.json"))
            ]

        self.assertIsInstance(result, BulkExportResult)
        self.assertEqual(result.messages_written, 3)
        self.assertEqual(sum(item["session"]["messageCount"] for item in payloads), 3)
        self.assertEqual(client.group_member_calls, [talker])
        self.assertEqual(result.group_roster_attempts, 1)
        self.assertEqual(result.group_roster_failures, 1)
        self.assertEqual(result.degraded_group_rosters, (talker,))
        self.assertEqual(
            backend.partial_failures,
            [f"export_chat_group_roster:{talker}:fallback=contacts:known_names=0"],
        )

    def test_bulk_bad_roster_window_stops_after_checkpoint_and_resumes(self) -> None:
        export_day = date(2026, 8, 25)
        sessions = [
            {
                "username": f"synthetic-health-{index}@chatroom",
                "displayName": f"Synthetic Health Group {index}",
            }
            for index in range(1, 7)
        ]
        messages = {
            session["username"]: [
                _message(export_day, index, f"wxid_member_{index}_placeholder")
            ]
            for index, session in enumerate(sessions, start=1)
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "bulk-raw"
            state_path = root / ".bulk-state.json"
            first_client = _RosterClient(
                sessions,
                messages,
                failed_rosters={session["username"] for session in sessions},
            )
            first_backend = WeflowApiBackend(_config(root))
            first_backend._client = first_client
            first_result = first_backend.export_chats_range(
                export_day,
                export_day,
                raw_root=raw_root,
                sessions=sessions,
                state_path=state_path,
            )

            first_payloads = sorted(raw_root.glob("*/*.json"))
            self.assertEqual(len(first_payloads), 5)
            self.assertFalse(any("Group 6" in str(path) for path in first_payloads))
            self.assertIn("重启 WeFlow API 服务后用原命令续跑", first_result.stopped_reason)
            self.assertTrue(state_path.is_file())
            self.assertTrue(first_result.reconciliation_path.is_file())
            reconciliation = json.loads(first_result.reconciliation_path.read_text(encoding="utf-8"))
            self.assertEqual(reconciliation["incompleteCoverageDays"], 1)

            resumed_client = _RosterClient(sessions, messages)
            resumed_backend = WeflowApiBackend(_config(root))
            resumed_backend._client = resumed_client
            resumed = resumed_backend.export_chats_range(
                export_day,
                export_day,
                raw_root=raw_root,
                sessions=sessions,
                state_path=state_path,
            )

            final_payloads = sorted(raw_root.glob("*/*.json"))

        self.assertIsInstance(resumed, BulkExportResult)
        self.assertEqual(len(final_payloads), 6)
        self.assertEqual(resumed.resumed_intervals, 5)
        self.assertEqual(
            [talker for talker, media in resumed_client.message_calls if media],
            [sessions[-1]["username"]],
        )

    def test_bulk_sparse_roster_failures_do_not_trip_health_signal(self) -> None:
        export_day = date(2026, 8, 25)
        sessions = [
            {
                "username": f"synthetic-sparse-{index}@chatroom",
                "displayName": f"Synthetic Sparse Group {index}",
            }
            for index in range(1, 7)
        ]
        messages = {
            session["username"]: [
                _message(export_day, index, f"wxid_member_{index}_placeholder")
            ]
            for index, session in enumerate(sessions, start=1)
        }
        failed = {sessions[0]["username"], sessions[4]["username"]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = WeflowApiBackend(_config(root))
            backend._client = _RosterClient(sessions, messages, failed_rosters=failed)
            result = backend.export_chats_range(
                export_day,
                export_day,
                raw_root=root / "bulk-raw",
                sessions=sessions,
                state_path=root / ".bulk-state.json",
            )

        self.assertIsInstance(result, BulkExportResult)
        self.assertEqual(result.messages_written, 6)
        self.assertEqual(result.group_roster_attempts, 6)
        self.assertEqual(result.group_roster_failures, 2)
        self.assertEqual(set(result.degraded_group_rosters), failed)


if __name__ == "__main__":
    unittest.main()
