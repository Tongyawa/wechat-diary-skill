from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from wechat_diary_core.backends.weflow_api.mapper import (
    map_moments_json,
    map_session_json,
    moments_filename,
    session_directory_name,
    write_session_export,
)
from wechat_diary_core.raw_schema import validate_moments_json, validate_session_json


FIXTURES = Path(__file__).parent / "fixtures" / "weflow_api"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class WeflowApiMapperTests(unittest.TestCase):
    def test_real_shape_messages_cover_server_id_empty_sender_quote_and_voice_fallback(self) -> None:
        sessions = _fixture("sessions.json")["sessions"]
        contacts = _fixture("contacts.json")["contacts"]
        messages = _fixture("messages.json")["messages"]
        timestamp = messages[0]["createTime"]
        system = {
            **copy.deepcopy(messages[0]),
            "localId": 2333,
            "serverId": "system-server-id",
            "localType": 10000,
            "isSend": 1,
            "senderUsername": "",
            "content": "系统提示",
        }
        emoji = {
            **copy.deepcopy(messages[0]),
            "localId": 2332,
            "serverId": "emoji-server-id",
            "localType": 47,
            "isSend": 0,
            "senderUsername": "wxid_contact_placeholder",
            "content": "[动画表情]",
            "replyToMessageId": "6277496717170092270",
            "quote": {
                "platformMessageId": "6277496717170092270",
                "sender": "wxid_self_placeholder",
                "accountName": "本人占位",
                "content": "被引用原文",
                "type": 1,
            },
        }
        voice = {
            **copy.deepcopy(messages[0]),
            "localId": 2331,
            "serverId": "voice-server-id",
            "localType": 34,
            "content": "[语音]",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            emoji_file = root / "source.gif"
            emoji_file.write_bytes(b"GIF89a")
            voice_file = root / "voice.wav"
            voice_file.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
            emoji.update({"mediaLocalPath": str(emoji_file), "mediaFileName": "source.gif", "mediaType": "emoji"})
            voice.update({"mediaLocalPath": str(voice_file), "mediaFileName": "voice.wav", "mediaType": "voice"})
            data = map_session_json(
                sessions[0],
                [messages[0], system, emoji, voice],
                contacts=contacts,
                self_wxids=["wxid_self_placeholder"],
                media_dir=root / "session" / "media",
                transcriber=None,
            )

        validate_session_json(data)
        self.assertEqual(data["messages"][0]["platformMessageId"], "6277496717170092270")
        self.assertEqual(data["messages"][0]["localId"], 2334)
        self.assertEqual(data["messages"][0]["formattedTime"], datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(data["messages"][1]["senderUsername"], "wxid_self_placeholder")
        self.assertEqual(data["messages"][2]["type"], "动画表情")
        self.assertEqual(data["messages"][2]["replyToMessageId"], "6277496717170092270")
        self.assertEqual(data["messages"][2]["quotedContent"], "被引用原文")
        self.assertNotEqual(data["messages"][3]["content"], "[语音]")
        self.assertIn("转文字失败", data["messages"][3]["content"])

    def test_empty_group_system_sender_falls_back_to_chatroom(self) -> None:
        message = copy.deepcopy(_fixture("messages.json")["messages"][0])
        message.update({"localType": 10000, "senderUsername": "", "isSend": 0})
        session = {"username": "sample@chatroom", "displayName": "示例群聊"}
        data = map_session_json(
            session,
            [message],
            contacts=_fixture("contacts.json")["contacts"],
            group_members=_fixture("group-members.json")["members"],
        )

        validate_session_json(data)
        self.assertEqual(data["messages"][0]["senderUsername"], "sample@chatroom")
        self.assertEqual(data["messages"][0]["senderDisplayName"], "示例群聊")

    def test_range_export_uses_type_prefix_and_range_suffix_and_validates(self) -> None:
        session = _fixture("sessions.json")["sessions"][0]
        messages = _fixture("messages.json")["messages"]
        contacts = _fixture("contacts.json")["contacts"]
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            destination = write_session_export(
                raw,
                session,
                messages,
                start=date(2026, 7, 1),
                end=date(2026, 8, 5),
                contacts=contacts,
                self_wxids=["wxid_self_placeholder"],
            )
            payload = json.loads((destination / f"{destination.name}.json").read_text(encoding="utf-8"))

        self.assertEqual(destination.name, "私聊_示例联系人_20260701-20260805")
        validate_session_json(payload)

    def test_session_failure_never_publishes_partial_live_directory(self) -> None:
        session = _fixture("sessions.json")["sessions"][0]
        message = copy.deepcopy(_fixture("messages.json")["messages"][0])
        message.update({"localType": 3, "mediaLocalPath": "Z:/missing/image.jpg", "mediaFileName": "image.jpg"})
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            expected = raw / session_directory_name(session, date(2026, 8, 5), date(2026, 8, 5))
            write_session_export(
                raw,
                session,
                _fixture("messages.json")["messages"],
                start=date(2026, 8, 5),
                end=date(2026, 8, 5),
                contacts=_fixture("contacts.json")["contacts"],
            )
            with self.assertRaises(FileNotFoundError):
                write_session_export(
                    raw,
                    session,
                    [message],
                    start=date(2026, 8, 5),
                    end=date(2026, 8, 5),
                    contacts=_fixture("contacts.json")["contacts"],
                )
            # A failed rerun leaves the previous complete live snapshot intact.
            self.assertTrue(expected.exists())
            preserved = json.loads((expected / f"{expected.name}.json").read_text(encoding="utf-8"))
            self.assertEqual(preserved["messages"][0]["type"], "文本消息")

    def test_moments_real_shape_filters_locally_and_passes_validator(self) -> None:
        post = _fixture("timeline.json")["timeline"][0]
        export_day = datetime.fromtimestamp(post["createTime"]).date()
        foreign = {**post, "id": "foreign", "tid": "foreign", "username": "wxid_foreign_placeholder"}
        data = map_moments_json(
            [post, foreign],
            usernames=["wxid_contact_placeholder"],
            start=export_day,
            end=export_day,
        )

        validate_moments_json(data)
        self.assertEqual(len(data["posts"]), 1)
        self.assertEqual(data["posts"][0]["username"], "wxid_contact_placeholder")
        filename = moments_filename(["wxid_contact_placeholder"], export_day)
        self.assertRegex(filename, rf"^朋友圈导出_{export_day:%Y%m%d}_[0-9a-f]{{8}}\.json$")
        self.assertNotIn("wxid", filename)


if __name__ == "__main__":
    unittest.main()
