from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from wechat_diary_core.backends.weflow_api.backend import WeflowApiBackend
from wechat_diary_core.backends.weflow_api.client import WeflowApiError
from wechat_diary_core.config import load_config
from wechat_diary_core.raw_schema import validate_session_json


def _config(root: Path):
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


class WeflowApiBackendTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
