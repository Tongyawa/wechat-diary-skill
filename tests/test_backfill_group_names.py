from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.backfill_group_names import BackfillError, backfill_group_names


def _write_canonical(path: Path, session_wxid: str, messages: list[dict]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session": {
            "wxid": session_wxid,
            "displayName": "Synthetic Group",
            "messageCount": len(messages),
        },
        "messages": messages,
        "weflow": {"format": "json", "source": "http_api"},
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(content)
    return content


def _message(
    local_id: int,
    sender: str,
    display_name: str,
    *,
    nickname: str = "",
    remark: str = "",
    quoted_sender: str = "",
    quoted_display_name: str = "",
) -> dict:
    return {
        "localId": local_id,
        "createTime": 1_700_000_000 + local_id,
        "type": "文本消息",
        "content": f"synthetic content {local_id}",
        "source": f"../_media/images/aa/synthetic-{local_id}.jpg",
        "isSend": 0,
        "senderUsername": sender,
        "senderDisplayName": display_name,
        "senderRemark": remark,
        "senderNickname": nickname,
        "quotedContent": f"quoted synthetic content {local_id}",
        "quotedSender": quoted_sender,
        "quotedSenderDisplayName": quoted_display_name,
        "fixtureExtra": {
            "mustStay": [1, 2, 3],
            "text": f'"senderDisplayName": "{sender}" must stay in content-like data',
        },
    }


class BackfillGroupNamesTests(unittest.TestCase):
    def test_apply_repairs_only_self_describing_fields_and_is_byte_monotonic(self) -> None:
        target_group = "synthetic-target@chatroom"
        trap_group = "synthetic-trap@chatroom"
        first_sender = "wxid_first_member_placeholder"
        quoted_sender = "wxid_quoted_member_placeholder"
        unresolved_sender = "wxid_unresolved_member_placeholder"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            known_path = root / "群聊_Synthetic Group_20260824" / "known.json"
            known_before = _write_canonical(
                known_path,
                target_group,
                [
                    _message(
                        1,
                        first_sender,
                        "Synthetic Known Member",
                        nickname="Synthetic Nickname",
                        remark="Synthetic Remark",
                    ),
                    _message(2, quoted_sender, "Synthetic Quoted Member"),
                ],
            )
            # 目录前缀相同但 session.wxid 不同；更新更晚也不得冒名成为目标群名册。
            trap_path = root / "群聊_Synthetic Group Extra_20260825" / "trap.json"
            trap_before = _write_canonical(
                trap_path,
                trap_group,
                [
                    _message(
                        999,
                        first_sender,
                        "Synthetic Impostor Name",
                        nickname="Synthetic Impostor Nickname",
                        remark="Synthetic Impostor Remark",
                    )
                ],
            )
            target_path = root / "群聊_Synthetic Group_20260825" / "degraded.json"
            before = _write_canonical(
                target_path,
                target_group,
                [
                    _message(
                        10,
                        first_sender,
                        first_sender,
                        quoted_sender=quoted_sender,
                        quoted_display_name=quoted_sender,
                    ),
                    _message(
                        11,
                        first_sender,
                        "Existing True Name",
                        nickname="Existing True Nickname",
                        remark="Existing True Remark",
                    ),
                    _message(12, unresolved_sender, unresolved_sender),
                ],
            )
            paths_before = {path.relative_to(root) for path in root.rglob("*")}

            preview = backfill_group_names(root, apply=False)
            self.assertEqual(target_path.read_bytes(), before)
            self.assertFalse(preview.applied)
            self.assertEqual(preview.affected_files, 1)
            self.assertEqual(preview.repaired_messages, 1)

            applied = backfill_group_names(root, apply=True)
            after = target_path.read_bytes()

            expected = before
            expected = expected.replace(
                f'"senderDisplayName": "{first_sender}"'.encode(),
                '"senderDisplayName": "Existing True Name"'.encode(),
                1,
            )
            expected = expected.replace(
                b'"senderRemark": ""',
                '"senderRemark": "Existing True Remark"'.encode(),
                1,
            )
            expected = expected.replace(
                b'"senderNickname": ""',
                '"senderNickname": "Existing True Nickname"'.encode(),
                1,
            )
            expected = expected.replace(
                f'"quotedSenderDisplayName": "{quoted_sender}"'.encode(),
                '"quotedSenderDisplayName": "Synthetic Quoted Member"'.encode(),
                1,
            )

            def assert_only_allowed_byte_edits(candidate: bytes) -> None:
                self.assertEqual(candidate, expected)

            # 阳性对照：会重排键并改动整文件字节的实现必须让判据变红。
            bad_rewrite = json.dumps(
                json.loads(after),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            with self.assertRaises(AssertionError):
                assert_only_allowed_byte_edits(bad_rewrite)
            assert_only_allowed_byte_edits(after)

            payload = json.loads(after)
            repaired, preserved, unresolved = payload["messages"]
            self.assertEqual(repaired["senderDisplayName"], "Existing True Name")
            self.assertEqual(repaired["senderNickname"], "Existing True Nickname")
            self.assertEqual(repaired["senderRemark"], "Existing True Remark")
            self.assertEqual(
                repaired["quotedSenderDisplayName"],
                "Synthetic Quoted Member",
            )
            self.assertEqual(preserved["senderDisplayName"], "Existing True Name")
            self.assertEqual(preserved["senderNickname"], "Existing True Nickname")
            self.assertEqual(preserved["senderRemark"], "Existing True Remark")
            self.assertEqual(unresolved["senderDisplayName"], unresolved_sender)
            self.assertNotIn(b"Synthetic Impostor", after)
            self.assertEqual(known_path.read_bytes(), known_before)
            self.assertEqual(trap_path.read_bytes(), trap_before)
            self.assertEqual(
                {path.relative_to(root) for path in root.rglob("*")},
                paths_before,
                "回填不得留下状态文件或临时文件",
            )

            self.assertTrue(applied.applied)
            self.assertEqual(applied.sender_display_names, 1)
            self.assertEqual(applied.sender_nicknames, 1)
            self.assertEqual(applied.sender_remarks, 1)
            self.assertEqual(applied.quoted_display_names, 1)
            self.assertEqual(applied.unresolved_sender_names, 1)

            second = backfill_group_names(root, apply=True)
            self.assertEqual(second.affected_files, 0)
            self.assertEqual(target_path.read_bytes(), after)

    def test_noncanonical_serialization_is_refused_without_writing(self) -> None:
        group = "synthetic-compact@chatroom"
        sender = "wxid_compact_member_placeholder"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_canonical(
                root / "群聊_Compact_20260824" / "known.json",
                group,
                [_message(1, sender, "Synthetic Compact Member")],
            )
            target = root / "群聊_Compact_20260825" / "compact.json"
            target.parent.mkdir(parents=True)
            payload = {
                "session": {"wxid": group},
                "messages": [_message(2, sender, sender)],
            }
            before = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            target.write_bytes(before)

            with self.assertRaisesRegex(BackfillError, "拒绝整文件重序列化"):
                backfill_group_names(root, apply=True)

            self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
