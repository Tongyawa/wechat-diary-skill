from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import Mock

from wechat_diary_core.backends.weflow_api.backend import WeflowApiBackend
from wechat_diary_core.backends.weflow_api.client import WeflowApiError
from wechat_diary_core.backends.weflow_api.failure_state import SessionFailureState
from wechat_diary_core.config import load_config
from wechat_diary_core.raw_schema import validate_moments_json, validate_session_json


def _config(root: Path, *, asr_engine: str = "", skip_official_accounts: bool = True):
    path = root / "config.toml"
    path.write_text(
        f"""
[user]
self_wxids = ["wxid_self_placeholder"]

[paths]
raw = "{(root / 'raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
archived = "{(root / 'archived').as_posix()}"
insights = "{(root / 'insights').as_posix()}"

[export_backend]
backend = "weflow_api"

[export_backend.weflow_api]
base_url = "http://127.0.0.1:5031"
access_token = "fixed-token"

[daily_export]
self_moments_usernames = []
skip_official_accounts = {str(skip_official_accounts).lower()}

[asr]
engine = "{asr_engine}"
""".strip(),
        encoding="utf-8",
    )
    return load_config(path)


class _Client:
    def __init__(self):
        self.health_calls = 0
        self.token_calls = 0

    def health(self):
        self.health_calls += 1

    def validate_token(self):
        self.token_calls += 1

    def fetch_sessions(self, *, limit):
        return [
            {"username": "wxid_ok_placeholder", "displayName": "成功会话"},
            {"username": "wxid_fail_placeholder", "displayName": "失败会话"},
        ]

    def fetch_contacts(self, *, limit):
        return [
            {"username": "wxid_ok_placeholder", "displayName": "成功会话", "nickname": "成功会话"},
            {"username": "wxid_self_placeholder", "displayName": "本人占位", "nickname": "本人占位"},
        ]

    def fetch_messages(self, talker, **kwargs):
        if talker == "wxid_fail_placeholder":
            raise RuntimeError("fixture failure")
        return [
            {
                "localId": 10,
                "serverId": "server-10",
                "localType": 1,
                "createTime": 1785921697,
                "isSend": 1,
                "senderUsername": "wxid_self_placeholder",
                "content": "示例文本",
                "rawContent": "",
                "parsedContent": "",
                "replyToMessageId": "",
                "quote": None,
            }
        ]

    def export_moments(self, output_dir, usernames, **kwargs):
        output = Path(output_dir)
        source = output / "朋友圈导出_2026-08-05T12-03-20.json"
        source.write_text(
            json.dumps(
                {
                    "exportTime": "2026-08-05 12:03:20",
                    "totalPosts": 1,
                    "filters": {"usernames": usernames},
                    "posts": [
                        {
                            "username": usernames[0],
                            "nickname": "示例联系人",
                            "createTime": 1785921697,
                            "createTimeStr": "2026/08/05 12:01:37",
                            "contentDesc": "媒体未解密的示例动态",
                            "type": 1,
                            "media": [{"url": "https://example.invalid/media/example.jpg"}],
                            "likes": [],
                            "comments": [],
                            "location": {"poiName": "", "address": ""},
                            "id": "post-placeholder",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {"success": True, "filePath": str(source), "postCount": 1, "mediaCount": 0}


class _EmptyMomentsClient(_Client):
    def export_moments(self, output_dir, usernames, **kwargs):
        return {"success": True, "filePath": "", "postCount": 0, "mediaCount": 0}


class _TrackingSessionClient:
    def __init__(self, sessions, *, failures=()):
        self.sessions = sessions
        self.failures = set(failures)
        self.message_calls = []

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

    def fetch_messages(self, talker, **kwargs):
        self.message_calls.append(talker)
        if talker in self.failures:
            raise RuntimeError("fixture cursor failure")
        return []


def _seed_failure_state(root: Path, wxid: str, *, ignored: bool = False) -> Path:
    path = root / ".export-state.json"
    state = SessionFailureState(path)
    for day in (4, 5, 6):
        state.record_failure(wxid, "审查会话占位", date(2026, 8, day), "fixture cursor failure")
    if ignored:
        state.ignore([wxid], authorized_date=date(2026, 8, 6))
    state.save()
    return path


class WeflowApiBackendTests(unittest.TestCase):
    def test_skip_official_accounts_controls_message_requests(self) -> None:
        sessions = [
            {"username": "gh_official_placeholder", "displayName": "公众号占位"},
            {"username": "wxid_contact_placeholder", "displayName": "联系人占位"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = _TrackingSessionClient(sessions)
            backend = WeflowApiBackend(_config(root))
            backend._client = client
            output = io.StringIO()
            with redirect_stdout(output):
                backend.export_chats(date(2026, 8, 6))

            self.assertEqual(client.message_calls, ["wxid_contact_placeholder"])
            self.assertIn("已跳过 1 个公众号会话", output.getvalue())
            self.assertEqual(backend.partial_failures, [])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = _TrackingSessionClient(sessions)
            backend = WeflowApiBackend(_config(root, skip_official_accounts=False))
            backend._client = client
            backend.export_chats(date(2026, 8, 6))

            self.assertEqual(client.message_calls, ["gh_official_placeholder", "wxid_contact_placeholder"])

    def test_unignored_failure_still_warns_and_is_partial(self) -> None:
        sessions = [
            {"username": "wxid_failed_placeholder", "displayName": "失败会话占位"},
            {"username": "wxid_ok_placeholder", "displayName": "成功会话占位"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = WeflowApiBackend(_config(root))
            backend._client = _TrackingSessionClient(sessions, failures={"wxid_failed_placeholder"})
            errors = io.StringIO()
            with redirect_stderr(errors):
                backend.export_chats(date(2026, 8, 6))

        self.assertEqual(backend.partial_failures, ["export_chat_session:wxid_failed_placeholder"])
        self.assertIn("[WARN]", errors.getvalue())
        self.assertIn("wxid_failed_placeholder", errors.getvalue())

    def test_ignored_failure_is_silent_partial_and_has_summary_and_runlog_detail(self) -> None:
        sessions = [{"username": "wxid_ignored_placeholder", "displayName": "忽略会话占位"}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_failure_state(root, "wxid_ignored_placeholder", ignored=True)
            backend = WeflowApiBackend(_config(root))
            backend._client = _TrackingSessionClient(sessions, failures={"wxid_ignored_placeholder"})
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                backend.export_chats(date(2026, 8, 7))
            log_text = next((root / ".runlog").glob("*-daily-export.log")).read_text(encoding="utf-8")

        self.assertEqual(backend.partial_failures, [])
        self.assertNotIn("[WARN]", errors.getvalue())
        self.assertIn("1 个已忽略会话仍导出失败", output.getvalue())
        self.assertIn("[IGNORED]", log_text)
        self.assertIn("wxid_ignored_placeholder", log_text)

    def test_ignored_session_success_clears_state_and_prints_recovery(self) -> None:
        sessions = [{"username": "wxid_recovered_placeholder", "displayName": "恢复会话占位"}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = _seed_failure_state(root, "wxid_recovered_placeholder", ignored=True)
            backend = WeflowApiBackend(_config(root))
            backend._client = _TrackingSessionClient(sessions)
            output = io.StringIO()
            with redirect_stdout(output):
                backend.export_chats(date(2026, 8, 7))
            restored = SessionFailureState.load(state_path)

        self.assertEqual(restored.failures, {})
        self.assertEqual(restored.ignored, {})
        self.assertIn("已恢复正常导出，已从忽略名单移除", output.getvalue())

    def test_review_noninteractive_does_not_modify_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = _seed_failure_state(root, "wxid_pending_placeholder")
            before = state_path.read_bytes()
            backend = WeflowApiBackend(_config(root))
            backend._failure_state = SessionFailureState.load(state_path)
            input_func = Mock(side_effect=AssertionError("noninteractive review must not prompt"))
            output = io.StringIO()

            with redirect_stdout(output):
                backend.review_session_failures(interactive=False, input_func=input_func)

            after = state_path.read_bytes()

        self.assertEqual(after, before)
        input_func.assert_not_called()
        self.assertIn("下次交互式运行", output.getvalue())

    def test_review_interactive_all_ignore_updates_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = _seed_failure_state(root, "wxid_pending_placeholder")
            backend = WeflowApiBackend(_config(root))
            backend._failure_state = SessionFailureState.load(state_path)
            backend._last_export_date = date(2026, 8, 6)
            input_func = Mock(return_value="a")

            with redirect_stdout(io.StringIO()):
                backend.review_session_failures(interactive=True, input_func=input_func)

            restored = SessionFailureState.load(state_path)

        input_func.assert_called_once_with("")
        self.assertIn("wxid_pending_placeholder", restored.ignored)
    def test_prepare_probes_health_and_protected_endpoint_without_owning_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _Client()
            backend = WeflowApiBackend(_config(Path(tmp)))
            backend._client = client
            backend.prepare()
            backend.shutdown()

        self.assertEqual(client.health_calls, 1)
        self.assertEqual(client.token_calls, 1)

    def test_prepare_rejects_bad_token_without_starting_or_waiting(self) -> None:
        class BadTokenClient:
            def health(self):
                return {"status": "ok"}

            def validate_token(self):
                raise WeflowApiError("unauthorized", status=401)

        with tempfile.TemporaryDirectory() as tmp:
            launches = []
            backend = WeflowApiBackend(
                _config(Path(tmp)),
                process_launcher=lambda path: launches.append(path),
                sleep=lambda seconds: self.fail("bad token must not poll"),
            )
            backend._client = BadTokenClient()
            with self.assertRaisesRegex(RuntimeError, "Token 不匹配"):
                backend.prepare()

        self.assertEqual(launches, [])

    def test_sensevoice_without_worker_python_degrades_before_process_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = WeflowApiBackend(_config(Path(tmp), asr_engine="sensevoice"))
            transcriber, reason = backend._asr()

        self.assertIsNone(transcriber)
        self.assertIn("worker_python", reason)

    def test_one_session_failure_is_isolated_and_successful_session_is_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            backend = WeflowApiBackend(cfg)
            backend._client = _Client()
            backend.export_chats(date(2026, 8, 5))
            exports = list(cfg.paths.raw.glob("私聊_成功会话_20260805/*.json"))
            payload = json.loads(exports[0].read_text(encoding="utf-8"))

        self.assertEqual(backend.partial_failures, ["export_chat_session:wxid_fail_placeholder"])
        self.assertEqual(len(exports), 1)
        validate_session_json(payload)

    def test_moments_media_failure_is_partial_and_canonical_json_still_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            backend = WeflowApiBackend(cfg)
            backend._client = _Client()
            backend.export_moments(["wxid_contact_placeholder"], date(2026, 8, 5))
            exports = list(cfg.paths.raw.glob("朋友圈导出_20260805_*.json"))
            payload = json.loads(exports[0].read_text(encoding="utf-8"))

        validate_moments_json(payload)
        self.assertEqual(len(exports), 1)
        self.assertEqual(len(backend.partial_failures), 1)
        self.assertTrue(backend.partial_failures[0].startswith("export_moments_media:"))
        self.assertNotIn("localPath", payload["posts"][0]["media"][0])
        self.assertIn("url", payload["posts"][0]["media"][0])

    def test_empty_moments_result_publishes_nothing_and_is_not_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            backend = WeflowApiBackend(cfg)
            backend._client = _EmptyMomentsClient()
            backend.partial_failures.append("existing-failure")

            backend.export_moments(["wxid_contact_placeholder"], date(2026, 8, 6))

            exports = list(cfg.paths.raw.glob("朋友圈导出_*.json"))
            staging_parent = cfg.paths.raw.parent / f".{cfg.paths.raw.name}.weflow-api-moments-staging"
            self.assertEqual(exports, [])
            self.assertEqual(backend.partial_failures, ["existing-failure"])
            self.assertFalse(staging_parent.exists())


if __name__ == "__main__":
    unittest.main()
