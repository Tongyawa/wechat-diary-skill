from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from scripts import export_on_demand as module
from wechat_diary_core.config import load_config


FIXTURES = Path(__file__).parent / "fixtures" / "weflow_api"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _config(root: Path) -> object:
    config_path = root / "config.toml"
    config_path.write_text(
        f"""
[user]
self_wxids = ["wxid_self_placeholder"]

[paths]
raw = "{(root / 'live-raw').as_posix()}"
processed = "{(root / 'live-processed').as_posix()}"
archived = "{(root / 'live-archived').as_posix()}"
insights = "{(root / 'live-insights').as_posix()}"

[export_backend]
backend = "weflow_api"

[export_backend.weflow_api]
access_token = "fixture-token"
media_localize = true

[preprocessing.group_context_window]
enabled = true
messages_before = 3
messages_after = 5
time_window_minutes = 15
anchor_keywords = []
""".strip(),
        encoding="utf-8",
    )
    return load_config(config_path)


class FixtureClient:
    def __init__(self, sessions, messages_by_talker, contacts=()):
        self.sessions = sessions
        self.messages_by_talker = messages_by_talker
        self.contacts = list(contacts)
        self.calls: list[tuple] = []

    def fetch_sessions(self, *, limit):
        self.calls.append(("sessions", limit))
        return self.sessions

    def fetch_contacts(self, *, limit):
        self.calls.append(("contacts", limit))
        return self.contacts

    def fetch_group_members(self, chatroom_id):
        self.calls.append(("group-members", chatroom_id))
        return [{"wxid": "wxid_group_member", "nickname": "群成员占位"}]

    def fetch_messages(self, talker, **kwargs):
        self.calls.append(("messages", talker, kwargs))
        return copy.deepcopy(self.messages_by_talker.get(talker, []))


def _private_fixture_messages() -> list[dict]:
    return copy.deepcopy(_fixture("messages.json")["messages"])


def _group_messages() -> list[dict]:
    base = {
        "localType": 1,
        "rawContent": "",
        "parsedContent": "",
        "replyToMessageId": "",
        "quote": None,
    }
    start = datetime(2026, 5, 14, 10, 0, 0)
    messages = []
    for index, content in enumerate(["far-unrelated-marker", "filler-1", "filler-2", "filler-3", "filler-4", "mine", "after"]):
        messages.append(
            {
                **base,
                "localId": index,
                "serverId": f"group-server-{index}",
                "createTime": int((start + timedelta(minutes=index)).timestamp()),
                "isSend": int(index == 5),
                "senderUsername": "wxid_self_placeholder" if index == 5 else "wxid_group_member",
                "content": content,
            }
        )
    messages[0]["createTime"] = int((start - timedelta(days=2)).timestamp())
    return messages


class ExportOnDemandTests(unittest.TestCase):
    def test_session_resolution_exact_display_name_and_ambiguous_exit(self) -> None:
        sessions = [
            {"username": "wxid_alpha_placeholder", "displayName": "示例甲"},
            {"username": "wxid_beta_placeholder", "displayName": "示例乙"},
            {"username": "wxid_beta_2_placeholder", "displayName": "示例乙备份"},
        ]

        self.assertEqual(module.resolve_session(sessions, "wxid_alpha_placeholder"), sessions[0])
        self.assertEqual(module.resolve_session(sessions, "示例甲"), sessions[0])

        output = io.StringIO()
        with redirect_stderr(output):
            with self.assertRaises(module.SessionSelectionError) as raised:
                module.resolve_session(sessions, "示例乙")
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertIn("wxid_beta_placeholder", str(raised.exception))
        self.assertIn("wxid_beta_2_placeholder", str(raised.exception))

    def test_range_and_single_day_directory_names_and_merged_output(self) -> None:
        session = {"username": "wxid_contact_placeholder", "displayName": "示例联系人"}
        contacts = _fixture("contacts.json")["contacts"]
        messages = _private_fixture_messages()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            client = FixtureClient([session], {session["username"]: messages}, contacts)

            ranged = module.export_on_demand(
                cfg,
                sessions=[session],
                client=client,
                session_query=session["username"],
                start=date(2026, 5, 1),
                end=date(2026, 7, 31),
                out_root=root / "ranged",
                merged=True,
            )
            single = module.export_on_demand(
                cfg,
                sessions=[session],
                client=client,
                session_query=session["username"],
                start=date(2026, 8, 5),
                end=date(2026, 8, 5),
                out_root=root / "single",
            )

            self.assertEqual(ranged.raw_session_dir.name, "私聊_示例联系人_20260501-20260731")
            self.assertEqual(single.raw_session_dir.name, "私聊_示例联系人_20260805")
            self.assertEqual(ranged.output_session_dir.name, ranged.raw_session_dir.name)
            self.assertTrue(ranged.merged_file and ranged.merged_file.is_file())

    def test_repeated_out_keeps_ranges_isolated_and_removes_empty_staging(self) -> None:
        session = {"username": "wxid_contact_placeholder", "displayName": "示例联系人"}
        contacts = _fixture("contacts.json")["contacts"]

        class RangeClient(FixtureClient):
            def fetch_messages(self, talker, **kwargs):
                message = _private_fixture_messages()[0]
                end = kwargs["end"]
                message["content"] = f"range-marker-{end:%Y%m%d}"
                return [message]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            out = root / "out"
            client = RangeClient([session], {}, contacts)
            archive_inputs: list[tuple[str, list[str]]] = []

            def archive_spy(raw_path, **kwargs):
                archive_inputs.append(
                    (Path(raw_path).name, sorted(path.name for path in Path(raw_path).iterdir() if path.is_dir()))
                )
                return module.archive(raw_path, **kwargs)

            first = module.export_on_demand(
                cfg,
                sessions=[session],
                client=client,
                session_query=session["username"],
                start=date(2026, 5, 12),
                end=date(2026, 5, 13),
                out_root=out,
                archive_fn=archive_spy,
            )
            first_text = first.diary_files[0].read_text(encoding="utf-8")

            second = module.export_on_demand(
                cfg,
                sessions=[session],
                client=client,
                session_query=session["username"],
                start=date(2026, 5, 12),
                end=date(2026, 5, 14),
                out_root=out,
                archive_fn=archive_spy,
            )

            first_dir = out / "私聊_示例联系人_20260512-20260513"
            second_dir = out / "私聊_示例联系人_20260512-20260514"
            self.assertFalse((out / "私聊_示例联系人").exists())
            self.assertEqual(
                sorted(path.name for path in out.iterdir() if path.is_dir()),
                ["_raw", first_dir.name, second_dir.name],
            )
            self.assertEqual([path.name for path in first_dir.iterdir()], ["2026-08-05.md"])
            self.assertEqual([path.name for path in second_dir.iterdir()], ["2026-08-05.md"])
            self.assertIn("range-marker-20260513", first_text)
            self.assertNotIn("range-marker-20260514", first_dir.joinpath("2026-08-05.md").read_text(encoding="utf-8"))
            self.assertIn("range-marker-20260514", second_dir.joinpath("2026-08-05.md").read_text(encoding="utf-8"))
            self.assertEqual([children for _, children in archive_inputs], [
                ["私聊_示例联系人_20260512-20260513"],
                ["私聊_示例联系人_20260512-20260514"],
            ])
            self.assertTrue(all(name.startswith("._od") and len(name) <= 8 for name, _ in archive_inputs))

    def test_long_path_oserror_returns_actionable_chinese_hint(self) -> None:
        session = {"username": "wxid_contact_placeholder", "displayName": "示例联系人"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            client = FixtureClient([session], {session["username"]: _private_fixture_messages()})
            long_path = str(root / ("nested_" * 30) / "media" / "images" / "fixture.png")
            error = FileNotFoundError(2, "No such file or directory", long_path)
            output = io.StringIO()
            with redirect_stderr(output):
                with mock.patch.object(module, "_make_client", return_value=client), mock.patch.object(
                    module, "write_session_export", side_effect=error
                ), mock.patch.object(module, "_windows_long_paths_enabled", return_value=False):
                    code = module.main(
                        [
                            "--config",
                            str(root / "config.toml"),
                            "--session",
                            session["username"],
                            "--start",
                            "20260805",
                            "--end",
                            "20260805",
                            "--out",
                            str(root / "out"),
                        ]
                    )

            self.assertEqual(code, 1)
            message = output.getvalue()
            self.assertIn("输出路径过长", message)
            self.assertIn("260", message)
            self.assertIn(str(len(long_path)), message)
            self.assertIn("更短的 --out", message)
            self.assertIn("启用 Windows 长路径", message)

    def test_existing_out_and_live_roots_are_preserved(self) -> None:
        session = {"username": "wxid_contact_placeholder", "displayName": "示例联系人"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            keep_out = root / "out" / "unrelated.txt"
            keep_out.parent.mkdir(parents=True)
            keep_out.write_text("keep", encoding="utf-8")
            live_files = {}
            for path in (cfg.paths.raw, cfg.paths.processed, cfg.paths.archived):
                marker = path / "keep.txt"
                marker.parent.mkdir(parents=True)
                marker.write_text("live", encoding="utf-8")
                live_files[path] = marker.read_text(encoding="utf-8")

            module.export_on_demand(
                cfg,
                sessions=[session],
                client=FixtureClient([session], {session["username"]: _private_fixture_messages()}),
                session_query=session["username"],
                start=date(2026, 8, 5),
                end=date(2026, 8, 5),
                out_root=root / "out",
            )

            self.assertEqual(keep_out.read_text(encoding="utf-8"), "keep")
            for path, content in live_files.items():
                self.assertEqual((path / "keep.txt").read_text(encoding="utf-8"), content)

    def test_group_window_defaults_off_and_can_be_enabled(self) -> None:
        session = {"username": "sample@chatroom", "displayName": "示例群聊"}
        members = _fixture("group-members.json")["members"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            client = FixtureClient([session], {session["username"]: _group_messages()}, members)
            default_result = module.export_on_demand(
                cfg,
                sessions=[session],
                client=client,
                session_query=session["username"],
                start=date(2026, 5, 12),
                end=date(2026, 5, 14),
                out_root=root / "default",
            )
            window_result = module.export_on_demand(
                cfg,
                sessions=[session],
                client=client,
                session_query=session["username"],
                start=date(2026, 5, 12),
                end=date(2026, 5, 14),
                out_root=root / "window",
                group_window=True,
            )

            default_text = "\n".join(path.read_text(encoding="utf-8") for path in default_result.diary_files)
            window_text = "\n".join(path.read_text(encoding="utf-8") for path in window_result.diary_files)
            self.assertIn("far-unrelated-marker", default_text)
            self.assertNotIn("far-unrelated-marker", window_text)
            self.assertNotEqual(default_text, window_text)

    def test_media_copy_toggle_keeps_references_and_changes_layout(self) -> None:
        session = {"username": "wxid_contact_placeholder", "displayName": "示例联系人"}
        message = _private_fixture_messages()[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "fixture-image.jpg"
            source.write_bytes(b"image-placeholder")
            message.update(
                {
                    "localType": 3,
                    "content": "[图片]",
                    "mediaLocalPath": str(source),
                    "mediaFileName": "fixture-image.jpg",
                    "mediaType": "image",
                }
            )
            cfg = _config(root)
            with_copy = module.export_on_demand(
                cfg,
                sessions=[session],
                client=FixtureClient([session], {session["username"]: [message]}),
                session_query=session["username"],
                start=date(2026, 8, 5),
                end=date(2026, 8, 5),
                out_root=root / "with-copy",
            )
            without_copy = module.export_on_demand(
                cfg,
                sessions=[session],
                client=FixtureClient([session], {session["username"]: [message]}),
                session_query=session["username"],
                start=date(2026, 8, 5),
                end=date(2026, 8, 5),
                out_root=root / "without-copy",
                copy_media=False,
            )

            media = with_copy.output_session_dir / "media" / "images" / "fixture-image.jpg"
            self.assertTrue(media.is_file())
            self.assertIn("[图片：media/images/fixture-image.jpg]", with_copy.diary_files[0].read_text(encoding="utf-8"))
            self.assertFalse((without_copy.output_session_dir / "media").exists())

    def test_real_shape_fixture_runs_through_raw_and_processed_outputs(self) -> None:
        session = _fixture("sessions.json")["sessions"][0]
        messages = _private_fixture_messages()
        contacts = _fixture("contacts.json")["contacts"]
        system = {**copy.deepcopy(messages[0]), "localId": 2333, "serverId": "system-server-id", "localType": 10000, "isSend": 1, "senderUsername": "", "content": "系统提示"}
        quoted = {**copy.deepcopy(messages[0]), "localId": 2332, "serverId": "quote-server-id", "localType": 1, "isSend": 0, "senderUsername": "wxid_contact_placeholder", "content": "引用消息", "replyToMessageId": messages[0]["serverId"], "quote": {"platformMessageId": messages[0]["serverId"], "sender": "wxid_self_placeholder", "accountName": "本人占位", "content": "被引用原文", "type": 1}}
        voice = {**copy.deepcopy(messages[0]), "localId": 2331, "serverId": "voice-server-id", "localType": 34, "content": "[语音]"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            result = module.export_on_demand(
                cfg,
                sessions=[session],
                client=FixtureClient([session], {session["username"]: [*messages, system, quoted, voice]}, contacts),
                session_query="示例联系人",
                start=date(2026, 8, 5),
                end=date(2026, 8, 5),
                out_root=root / "out",
                enable_asr=False,
            )
            payload = json.loads((result.raw_session_dir / f"{result.raw_session_dir.name}.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["messages"][0]["platformMessageId"], "6277496717170092270")
            self.assertIn("quotedContent", json.dumps(payload, ensure_ascii=False))
            self.assertIn("转文字失败", json.dumps(payload, ensure_ascii=False))
            self.assertTrue(result.diary_files)

    def test_main_multi_match_returns_exit_code_two_and_prints_candidates(self) -> None:
        sessions = [
            {"username": "wxid_same_a_placeholder", "displayName": "同名候选"},
            {"username": "wxid_same_b_placeholder", "displayName": "同名候选"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            client = FixtureClient(sessions, {})
            output = io.StringIO()
            with redirect_stderr(output):
                with mock.patch.object(module, "_make_client", return_value=client):
                    code = module.main(["--config", str(root / "config.toml"), "--session", "同名候选", "--start", "20260805", "--end", "20260805", "--out", str(root / "out")])
            self.assertEqual(code, 2)
            self.assertIn("wxid_same_a_placeholder", output.getvalue())
            self.assertIn("wxid_same_b_placeholder", output.getvalue())


if __name__ == "__main__":
    unittest.main()
