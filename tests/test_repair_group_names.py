from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.repair_group_names import (
    RECOMPUTE_REPORT_FILENAME,
    ROSTER_INDEX_FILENAME,
    SNAPSHOT_FILENAME,
    GroupNameRepairError,
    InterCallPacer,
    _assert_only_name_fields_changed,
    prepare_group_name_repair,
    recompute_group_names,
    rescan_group_names,
)
from wechat_diary_core.backends.weflow_api.client import (
    GROUP_ROSTER_RETRY_COOLDOWN_SECONDS,
)
from wechat_diary_core.raw_schema import validate_session_json


REAL_ROSTER_MEMBER_KEYS = {
    "alias",
    "avatarUrl",
    "displayName",
    "groupNickname",
    "isFriend",
    "isOwner",
    "messageCount",
    "nickname",
    "remark",
    "wxid",
}


def _member(
    wxid: str,
    *,
    remark: str = "",
    group_nickname: str = "",
    nickname: str = "",
) -> dict:
    return {
        "wxid": wxid,
        "displayName": "Synthetic API Display",
        "nickname": nickname,
        "remark": remark,
        "alias": "synthetic_alias",
        "groupNickname": group_nickname,
        "avatarUrl": "",
        "isOwner": False,
        "isFriend": True,
        "messageCount": 0,
    }


def _message(
    local_id: int,
    sender: str,
    display_name: str,
    *,
    nickname: str = "old nickname",
    remark: str = "old remark",
    quoted_sender: str = "",
    quoted_display: str = "",
) -> dict:
    return {
        "localId": local_id,
        "platformMessageId": f"synthetic-platform-{local_id}",
        "createTime": 1_700_000_000 + local_id,
        "formattedTime": f"2026-08-30 12:00:{local_id:02d}",
        "type": "文本消息",
        "content": f"synthetic content {local_id}",
        "source": "",
        "isSend": 0,
        "senderUsername": sender,
        "senderDisplayName": display_name,
        "senderRemark": remark,
        "senderNickname": nickname,
        "quotedContent": f"synthetic quote {local_id}",
        "quotedSender": quoted_sender,
        "quotedSenderDisplayName": quoted_display,
        "fixtureExtra": {"mustStay": [1, 2, 3]},
    }


def _write_canonical(
    raw_root: Path,
    group: str,
    messages: list[dict],
    *,
    file_name: str = "2026-08-30.json",
) -> tuple[Path, bytes]:
    path = raw_root / f"群聊_Synthetic_{group.split('@')[0]}" / file_name
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session": {
            "wxid": group,
            "nickname": "Synthetic Group",
            "remark": "",
            "displayName": "Synthetic Group",
            "type": "group",
            "messageCount": len(messages),
        },
        "messages": messages,
        "weflow": {"format": "json", "source": "http_api"},
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(content)
    return path, content


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _SequenceRosterClient:
    def __init__(
        self,
        responses: dict[str, list[object]],
        *,
        evidence_dir: Path | None = None,
        clock: _FakeClock | None = None,
    ) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.evidence_dir = evidence_dir
        self.clock = clock
        self.calls: list[tuple[str, float]] = []

    def fetch_group_members(self, chatroom_id: str):
        if self.evidence_dir is not None:
            if not (self.evidence_dir / SNAPSHOT_FILENAME).is_file():
                raise AssertionError("名册请求早于降级清单落盘")
        self.calls.append((chatroom_id, self.clock.now if self.clock else 0.0))
        response = self.responses[chatroom_id].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class RepairGroupNamesTests(unittest.TestCase):
    def test_anonymous_fixture_matches_real_roster_keys_and_canonical_has_no_tail_newline(self) -> None:
        roster = [_member("wxid_fixture_member_placeholder", nickname="Synthetic Nick")]
        self.assertEqual(set(roster[0]), REAL_ROSTER_MEMBER_KEYS)

        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            path, content = _write_canonical(
                raw,
                "synthetic-fixture@chatroom",
                [_message(1, "wxid_fixture_member_placeholder", "Synthetic Nick")],
            )
            self.assertFalse(content.endswith(b"\n"))
            self.assertFalse(path.read_bytes().endswith(b"\n"))
            validate_session_json(json.loads(content))

    def test_prepare_defaults_to_dry_run_without_network_or_files(self) -> None:
        group = "synthetic-preview@chatroom"
        sender = "wxid_preview_member_placeholder"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "raw"
            evidence = base / "evidence"
            _write_canonical(raw, group, [_message(1, sender, sender)])
            client = _SequenceRosterClient({group: [AssertionError("must not call")]})

            report = prepare_group_name_repair(
                raw,
                evidence,
                apply=False,
                client=client,
            )

            self.assertFalse(report.applied)
            self.assertEqual(report.degraded_messages, 1)
            self.assertEqual(client.calls, [])
            self.assertFalse(evidence.exists())

            repository_evidence = Path(__file__).resolve().parents[1] / "tests" / "_private"
            with self.assertRaisesRegex(GroupNameRepairError, "公开代码仓"):
                prepare_group_name_repair(
                    raw,
                    repository_evidence,
                    apply=False,
                    client=client,
                )

    def test_prepare_persists_snapshot_before_api_and_paces_every_retry_and_group(self) -> None:
        groups = [
            "synthetic-a@chatroom",
            "synthetic-b@chatroom",
            "synthetic-c@chatroom",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "raw"
            evidence = base / "evidence"
            raw_before: dict[str, tuple[Path, bytes]] = {}
            for index, group in enumerate(groups, start=1):
                sender = f"wxid_member_{index}_placeholder"
                raw_before[group] = _write_canonical(
                    raw,
                    group,
                    [_message(index, sender, sender)],
                )
            clock = _FakeClock()
            client = _SequenceRosterClient(
                {
                    groups[0]: [
                        RuntimeError("synthetic timeout 1"),
                        RuntimeError("synthetic timeout 2"),
                        [_member("wxid_member_1_placeholder", nickname="Synthetic One")],
                    ],
                    groups[1]: [[], [], []],
                    groups[2]: [
                        RuntimeError("synthetic exhausted 1"),
                        RuntimeError("synthetic exhausted 2"),
                        RuntimeError("synthetic exhausted 3"),
                    ],
                },
                evidence_dir=evidence,
                clock=clock,
            )
            pacer = InterCallPacer(
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            )

            report = prepare_group_name_repair(
                raw,
                evidence,
                apply=True,
                client=client,
                pacer=pacer,
            )

            self.assertTrue((evidence / SNAPSHOT_FILENAME).is_file())
            self.assertTrue((evidence / ROSTER_INDEX_FILENAME).is_file())
            self.assertEqual(len(client.calls), 9)
            call_times = [item[1] for item in client.calls]
            self.assertTrue(
                all(
                    later - earlier >= GROUP_ROSTER_RETRY_COOLDOWN_SECONDS
                    for earlier, later in zip(call_times, call_times[1:])
                )
            )
            self.assertEqual(
                clock.sleeps,
                [GROUP_ROSTER_RETRY_COOLDOWN_SECONDS] * 8,
            )
            self.assertEqual(report.roster_successes, 1)
            self.assertEqual(report.empty_rosters, 1)
            self.assertEqual(report.exhausted_failures, 1)
            index = json.loads((evidence / ROSTER_INDEX_FILENAME).read_text("utf-8"))
            self.assertTrue(index["complete"])
            self.assertEqual(
                [item["status"] for item in index["groups"]],
                ["success", "empty_roster", "exhausted_failure"],
            )
            self.assertEqual(
                len(list(evidence.glob("group-members-*.json"))),
                1,
                "空名册与耗尽失败必须跳过快照，只在索引报告中留痕",
            )
            recomputed = recompute_group_names(raw, evidence, apply=True)
            self.assertEqual(recomputed.affected_files, 1)
            for group in groups[1:]:
                path, before = raw_before[group]
                self.assertEqual(
                    path.read_bytes(),
                    before,
                    "空名册与重试耗尽群不得进入离线重算",
                )

    def test_recompute_requires_snapshot_before_any_raw_write(self) -> None:
        group = "synthetic-guard@chatroom"
        sender = "wxid_guard_member_placeholder"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "raw"
            evidence = base / "evidence"
            evidence.mkdir()
            path, before = _write_canonical(raw, group, [_message(1, sender, sender)])

            with self.assertRaisesRegex(GroupNameRepairError, "步骤 ① 未完成"):
                recompute_group_names(raw, evidence, apply=True)

            self.assertEqual(path.read_bytes(), before)

    def test_recompute_overwrites_roster_fields_preserves_true_quote_and_is_idempotent(self) -> None:
        group = "synthetic-recompute@chatroom"
        first = "wxid_first_member_placeholder"
        second = "wxid_second_member_placeholder"
        unknown = "wxid_unknown_member_placeholder"
        roster = [
            _member(
                first,
                remark="Synthetic Current Remark",
                group_nickname="Synthetic Current Group Name",
                nickname="Synthetic Current Nickname",
            ),
            _member(
                second,
                group_nickname="Synthetic Second Group Name",
                nickname="Synthetic Second Nickname",
            ),
        ]
        messages = [
            _message(
                1,
                first,
                "Synthetic Stale Display",
                quoted_sender=second,
                quoted_display="Synthetic Historical Quote",
            ),
            _message(
                2,
                second,
                second,
                quoted_sender=first,
                quoted_display=first,
            ),
            _message(3, unknown, unknown),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "raw"
            evidence = base / "evidence"
            path, before = _write_canonical(raw, group, messages)
            client = _SequenceRosterClient({group: [roster]}, evidence_dir=evidence)
            prepare_group_name_repair(raw, evidence, apply=True, client=client)
            snapshot = json.loads((evidence / SNAPSHOT_FILENAME).read_text("utf-8"))
            self.assertEqual(len(snapshot["degradedMessages"]), 2)

            preview = recompute_group_names(raw, evidence, apply=False)
            self.assertFalse(preview.applied)
            self.assertEqual(path.read_bytes(), before)

            applied = recompute_group_names(raw, evidence, apply=True)
            after = path.read_bytes()
            self.assertFalse(after.endswith(b"\n"))
            self.assertTrue((evidence / RECOMPUTE_REPORT_FILENAME).is_file())
            payload = json.loads(after)
            validate_session_json(payload)
            first_message, second_message, unknown_message = payload["messages"]

            self.assertEqual(first_message["senderDisplayName"], "Synthetic Current Remark")
            self.assertEqual(first_message["senderRemark"], "Synthetic Current Remark")
            self.assertEqual(first_message["senderNickname"], "Synthetic Current Nickname")
            self.assertEqual(
                first_message["senderGroupNickname"],
                "Synthetic Current Group Name",
            )
            self.assertEqual(
                first_message["quotedSenderDisplayName"],
                "Synthetic Historical Quote",
                "引用时刻已有真值不得被现时名册覆盖",
            )
            self.assertEqual(
                second_message["senderDisplayName"],
                "Synthetic Second Group Name",
            )
            self.assertEqual(
                second_message["quotedSenderDisplayName"],
                "Synthetic Current Remark",
            )
            self.assertEqual(unknown_message, messages[2])
            self.assertNotIn("senderGroupNickname", unknown_message)
            self.assertGreater(applied.affected_files, 0)

            original_payload = json.loads(before)
            normalized_after = deepcopy(payload)
            for old, new in zip(original_payload["messages"], normalized_after["messages"]):
                for field in (
                    "senderDisplayName",
                    "senderRemark",
                    "senderNickname",
                    "senderGroupNickname",
                    "quotedSenderDisplayName",
                ):
                    if field in old:
                        new[field] = old[field]
                    else:
                        new.pop(field, None)
            self.assertEqual(normalized_after, original_payload)

            mutated = deepcopy(payload)
            mutated["messages"][0]["content"] = "forbidden mutation"
            with self.assertRaisesRegex(GroupNameRepairError, "允许字段之外"):
                _assert_only_name_fields_changed(payload, mutated, path)

            second_run = recompute_group_names(raw, evidence, apply=True)
            self.assertEqual(second_run.affected_files, 0)
            self.assertEqual(path.read_bytes(), after)

            residual = rescan_group_names(raw, evidence, write_report=True)
            self.assertEqual(residual["totals"]["messages"], 3)
            self.assertEqual(residual["totals"]["unresolvedSenderMessages"], 1)
            self.assertAlmostEqual(residual["totals"]["residualRate"], 1 / 3)


if __name__ == "__main__":
    unittest.main()
