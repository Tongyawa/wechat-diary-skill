from __future__ import annotations

import copy
import unittest

from wechat_diary_core.raw_schema import RawSchemaError, validate_moments_json, validate_session_json


def _session_payload() -> dict:
    return {
        "weflow": {},
        "session": {
            "wxid": "wxid_peer",
            "nickname": "Peer",
            "remark": "",
            "displayName": "Peer",
            "type": "私聊",
        },
        "messages": [
            {
                "localId": 1,
                "createTime": 1778839200,
                "formattedTime": "2026-05-15 10:00:00",
                "type": "引用消息",
                "content": "current text",
                "source": "",
                "isSend": 0,
                "senderUsername": "wxid_peer",
                "senderDisplayName": "Peer",
                "platformMessageId": "p1",
                "replyToMessageId": "p0",
                "quotedContent": "quoted text",
                "quotedSender": "Me",
                "quotedSenderDisplayName": "Me",
                "replyContext": {
                    "isSend": 1,
                    "senderDisplayName": "Me",
                    "senderUsername": "wxid_me",
                    "content": "quoted text",
                    "type": "文本消息",
                },
            }
        ],
    }


def _moments_payload() -> dict:
    return {
        "exportTime": "2026-05-15T00:00:00",
        "totalPosts": 1,
        "filters": {"usernames": ["wxid_peer"], "keyword": ""},
        "posts": [
            {
                "id": "post-1",
                "username": "wxid_peer",
                "nickname": "Peer",
                "createTime": 1778839200,
                "createTimeStr": "2026/05/15 10:00:00",
                "contentDesc": "hello",
                "type": 1,
                "media": [{"localPath": "media/post-1.jpg"}],
                "likes": [],
                "comments": [{"nickname": "Friend", "content": "nice", "refNickname": ""}],
                "location": {"poiName": "Somewhere", "cityName": "City"},
            }
        ],
    }


class RawSchemaTests(unittest.TestCase):
    def test_complete_session_payload_passes(self) -> None:
        validate_session_json(_session_payload())

    def test_session_missing_required_field_reports_field_name(self) -> None:
        payload = _session_payload()
        payload["messages"][0].pop("createTime")

        with self.assertRaisesRegex(RawSchemaError, r"messages\[0\]\.createTime"):
            validate_session_json(payload)

    def test_session_message_create_time_must_not_decrease(self) -> None:
        payload = _session_payload()
        later_message = copy.deepcopy(payload["messages"][0])
        later_message.update({"localId": 2, "createTime": 1778839199})
        payload["messages"].append(later_message)

        with self.assertRaisesRegex(RawSchemaError, r"顺序错误.*重新导出"):
            validate_session_json(payload)

    def test_fully_descending_session_reports_one_order_issue(self) -> None:
        payload = _session_payload()
        template = payload["messages"][0]
        payload["messages"] = []
        for index, create_time in enumerate(range(6, 0, -1), start=1):
            message = copy.deepcopy(template)
            message.update({"localId": index, "createTime": create_time})
            payload["messages"].append(message)

        with self.assertRaises(RawSchemaError) as context:
            validate_session_json(payload)

        error = str(context.exception)
        self.assertEqual(error.count("顺序错误"), 1)
        self.assertIn("messages[1].createTime", error)
        self.assertIn("createTime 6 → 5", error)
        self.assertIn("共 5 处", error)
        self.assertIn("请重新导出", error)

    def test_session_messages_with_equal_create_time_pass(self) -> None:
        payload = _session_payload()
        same_time_message = copy.deepcopy(payload["messages"][0])
        same_time_message["localId"] = 2
        payload["messages"].append(same_time_message)

        validate_session_json(payload)

    def test_all_text_bodies_missing_is_one_bounded_actionable_error(self) -> None:
        payload = _session_payload()
        template = payload["messages"][0]
        payload["messages"] = []
        for index in range(500):
            message = copy.deepcopy(template)
            message.update(
                {
                    "localId": index,
                    "createTime": 1778839200 + index,
                    "type": "文本消息",
                    "content": "",
                }
            )
            for field in (
                "replyContext",
                "replyToMessageId",
                "quotedContent",
                "quotedSender",
                "quotedSenderDisplayName",
            ):
                message.pop(field, None)
            payload["messages"].append(message)

        with self.assertRaises(RawSchemaError) as captured:
            validate_session_json(payload)

        error = str(captured.exception)
        self.assertEqual(error.count("文本消息"), 1)
        self.assertIn("共 500 条但全部缺少可渲染载荷", error)
        self.assertIn("检查导出 mapper", error)
        self.assertLess(len(error), 240)

    def test_sparse_empty_text_does_not_reject_other_valid_text(self) -> None:
        payload = _session_payload()
        valid = copy.deepcopy(payload["messages"][0])
        valid.update({"localId": 2, "createTime": 1778839201, "type": "文本消息", "content": "valid text"})
        empty = copy.deepcopy(payload["messages"][0])
        empty.update({"localId": 3, "createTime": 1778839202, "type": "文本消息", "content": ""})
        for field in (
            "replyContext",
            "replyToMessageId",
            "quotedContent",
            "quotedSender",
            "quotedSenderDisplayName",
        ):
            empty.pop(field, None)
        payload["messages"].extend([valid, empty])

        validate_session_json(payload)

    def test_references_do_not_mask_all_plain_text_bodies_missing(self) -> None:
        payload = _session_payload()
        reply = payload["messages"][0]
        second_reply = copy.deepcopy(reply)
        second_reply.update({"localId": 2, "createTime": 1778839201, "content": "another reply"})
        empty_texts = []
        for index in range(3, 8):
            message = copy.deepcopy(reply)
            message.update(
                {
                    "localId": index,
                    "createTime": 1778839200 + index,
                    "type": "文本消息",
                    "content": "",
                }
            )
            for field in (
                "replyContext",
                "replyToMessageId",
                "quotedContent",
                "quotedSender",
                "quotedSenderDisplayName",
            ):
                message.pop(field, None)
            empty_texts.append(message)
        payload["messages"] = [reply, second_reply, *empty_texts]

        with self.assertRaisesRegex(RawSchemaError, r"文本消息共 5 条但全部缺少可渲染载荷"):
            validate_session_json(payload)

    def test_reply_target_metadata_does_not_replace_reply_body(self) -> None:
        payload = _session_payload()
        first = payload["messages"][0]
        first["content"] = ""
        second = copy.deepcopy(first)
        second.update({"localId": 2, "createTime": 1778839201})
        payload["messages"] = [first, second]

        with self.assertRaisesRegex(RawSchemaError, r"引用消息正文共 2 条.*mapper"):
            validate_session_json(payload)

    def test_media_contract_accepts_paths_or_explicit_degradation_markers(self) -> None:
        payload = _session_payload()
        template = payload["messages"][0]
        image = copy.deepcopy(template)
        image.update({"localId": 2, "createTime": 1778839201, "type": "图片消息", "content": "[图片]"})
        voice = copy.deepcopy(template)
        voice.update(
            {
                "localId": 3,
                "createTime": 1778839202,
                "type": "语音消息",
                "content": "",
                "source": "media/voice/example.silk",
            }
        )
        emoji = copy.deepcopy(template)
        emoji.update({"localId": 4, "createTime": 1778839203, "type": "动画表情", "content": "", "source": ""})
        payload["messages"] = [image, voice, emoji]

        validate_session_json(payload)

    def test_all_voice_payloads_missing_is_rejected(self) -> None:
        payload = _session_payload()
        message = payload["messages"][0]
        message.update({"type": "语音消息", "content": "", "source": ""})

        with self.assertRaisesRegex(RawSchemaError, r"语音消息共 1 条.*content/source"):
            validate_session_json(payload)

    def test_complete_moments_payload_passes(self) -> None:
        validate_moments_json(_moments_payload())

    def test_moments_missing_required_field_reports_field_name(self) -> None:
        payload = copy.deepcopy(_moments_payload())
        payload["posts"][0].pop("contentDesc")

        with self.assertRaisesRegex(RawSchemaError, r"posts\[0\]\.contentDesc"):
            validate_moments_json(payload)


if __name__ == "__main__":
    unittest.main()
