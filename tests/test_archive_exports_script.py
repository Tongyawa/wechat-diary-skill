from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.archive_exports import MAX_REPORTED_GUARD_FAILURES
from scripts.archive_exports import main as archive_exports_main


def _write_config(root: Path) -> Path:
    config_path = root / "config.toml"
    config_path.write_text(
        f"""
[paths]
raw = "{(root / 'raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
archived = "{(root / 'archived').as_posix()}"

[automation]
weflow_exe = "{(root / 'WeFlow.exe').as_posix()}"
""".strip(),
        encoding="utf-8",
    )
    return config_path


def _message(
    local_id: int,
    create_time: int,
    platform_message_id: str | None,
    *,
    content: str | None = None,
) -> dict:
    return {
        "localId": local_id,
        "createTime": create_time,
        "formattedTime": "2026-05-15 10:00:00",
        "type": "文本消息",
        "content": content or f"message-{local_id}",
        "source": "",
        "isSend": 0,
        "senderUsername": "wxid_a",
        "senderDisplayName": "A",
        "platformMessageId": platform_message_id,
    }


def _chat_payload(
    *,
    session_wxid: str = "wxid_a",
    lineage: str = "http_api",
    legacy_version: str = "1.0.3",
    exported_at: int = 1781357387,
    messages: list[dict] | None = None,
) -> dict:
    if lineage == "http_api":
        weflow = {"format": "json", "source": "http_api"}
    elif lineage == "legacy_gui":
        weflow = {
            "generator": "WeFlow",
            "version": legacy_version,
            "exportedAt": exported_at,
        }
    else:
        weflow = {}
    return {
        "weflow": weflow,
        "session": {
            "wxid": session_wxid,
            "nickname": "A",
            "remark": "",
            "displayName": "A",
            "type": "私聊",
        },
        "messages": messages
        if messages is not None
        else [_message(1, 1778839200, "p1", content="hello")],
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_raw_collision(root: Path, incoming: dict, archived: dict) -> tuple[Path, Path, Path]:
    raw_source = root / "snapshot-raw"
    filename = "私聊_A_20260611.json"
    source_path = raw_source / "私聊_A_20260611" / filename
    archived_path = root / "archived" / "raw" / "私聊_A" / filename
    _write_json(source_path, incoming)
    _write_json(archived_path, archived)
    return raw_source, source_path, archived_path


def _run_silently(args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = archive_exports_main(args)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class ArchiveExportsScriptTests(unittest.TestCase):
    def test_default_ingest_copies_raw_and_processed_and_keeps_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            raw_source = root / "WeFlow-raw-exports-去年"
            raw_file = raw_source / "私聊_A_20250606-20260606" / "私聊_A_20250606-20260606.json"
            _write_json(raw_file, _chat_payload())
            processed_source = root / "WeFlow-processed-exports-去年"
            processed_file = processed_source / "私聊_A" / "2025-07-01.md"
            processed_file.parent.mkdir(parents=True)
            processed_file.write_text("hi", encoding="utf-8")

            exit_code, _, _ = _run_silently(
                [
                    "--config",
                    str(config_path),
                    "--raw-root",
                    str(raw_source),
                    "--processed-root",
                    str(processed_source),
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue((root / "archived" / "raw" / "私聊_A" / raw_file.name).exists())
            self.assertTrue((root / "archived" / "processed" / "私聊_A" / processed_file.name).exists())
            self.assertTrue(raw_file.exists())
            self.assertTrue(processed_file.exists())

    def test_move_source_removes_successfully_ingested_trees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            raw_source = root / "snapshot-raw"
            _write_json(raw_source / "私聊_A_20260611" / "私聊_A_20260611.json", _chat_payload())
            processed_source = root / "snapshot-processed"
            processed_file = processed_source / "私聊_A" / "2026-06-11.md"
            processed_file.parent.mkdir(parents=True)
            processed_file.write_text("hi", encoding="utf-8")

            exit_code, _, _ = _run_silently(
                [
                    "--config",
                    str(config_path),
                    "--raw-root",
                    str(raw_source),
                    "--processed-root",
                    str(processed_source),
                    "--move-source",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertFalse(raw_source.exists())
            self.assertFalse(processed_source.exists())

    def test_keep_source_is_a_compatible_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            source = root / "snapshot-processed"
            source_file = source / "私聊_A" / "2025-07-01.md"
            source_file.parent.mkdir(parents=True)
            source_file.write_text("hi", encoding="utf-8")

            exit_code, _, _ = _run_silently(
                ["--config", str(config_path), "--processed-root", str(source), "--keep-source"]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(source_file.exists())
            self.assertTrue((root / "archived" / "processed" / "私聊_A" / source_file.name).exists())

    def test_raw_schema_failure_skips_bad_json_and_keeps_source_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            raw_source = root / "snapshot-raw"
            good = raw_source / "私聊_A_20260611" / "私聊_A_20260611.json"
            bad = raw_source / "私聊_B_20260611" / "私聊_B_20260611.json"
            _write_json(good, _chat_payload(session_wxid="wxid_a"))
            invalid = _chat_payload(session_wxid="wxid_b")
            invalid["session"].pop("wxid")
            _write_json(bad, invalid)

            exit_code, _, stderr = _run_silently(
                ["--config", str(config_path), "--raw-root", str(raw_source), "--move-source"]
            )

            self.assertEqual(exit_code, 1)
            self.assertTrue((root / "archived" / "raw" / "私聊_A" / good.name).exists())
            self.assertFalse((root / "archived" / "raw" / "私聊_B" / bad.name).exists())
            self.assertTrue(raw_source.exists())
            self.assertIn("session.wxid", stderr)

    def test_positive_control_rejects_known_old_snapshot_before_it_can_overwrite_new(self) -> None:
        """The guard must turn red on the exact old-overwrites-new failure it claims to prevent."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            older = _chat_payload(messages=[_message(1, 100, "p1")])
            newer = _chat_payload(messages=[_message(1, 100, "p1"), _message(2, 200, "p2")])
            raw_source, _, archived_path = _write_raw_collision(root, older, newer)
            archived_before = archived_path.read_bytes()

            exit_code, stdout, stderr = _run_silently(
                ["--config", str(config_path), "--raw-root", str(raw_source)]
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(archived_path.read_bytes(), archived_before)
            self.assertTrue(raw_source.exists())
            self.assertNotIn("Merged", stdout)
            self.assertIn("OVERWRITE GUARD REJECTED", stderr)
            self.assertIn("incoming 时间水位更旧", stderr)
            self.assertIn("No files were written", stderr)

    def test_real_legacy_lineage_blocks_old_snapshot_even_with_force_overwrite(self) -> None:
        """Real legacy metadata must select the same-lineage evidence path, not the forceable fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            older = _chat_payload(
                lineage="legacy_gui",
                legacy_version="1.0.3",
                messages=[_message(1, 100, None), _message(2, 200, "p2")],
            )
            newer = _chat_payload(
                lineage="legacy_gui",
                legacy_version="9.7.0",
                messages=[
                    _message(1, 100, None),
                    _message(2, 200, "p2"),
                    _message(3, 300, "p3"),
                ],
            )
            raw_source, _, archived_path = _write_raw_collision(root, older, newer)
            archived_before = archived_path.read_bytes()

            exit_code, _, stderr = _run_silently(
                [
                    "--config",
                    str(config_path),
                    "--raw-root",
                    str(raw_source),
                    "--force-overwrite",
                ]
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(archived_path.read_bytes(), archived_before)
            self.assertIn("incoming 时间水位更旧", stderr)
            self.assertIn("已有回退证据，禁止强制覆盖", stderr)

    def test_legacy_exported_at_rejects_older_snapshot_when_messages_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            messages = [_message(1, 100, None), _message(2, 200, "p2")]
            incoming = _chat_payload(
                lineage="legacy_gui",
                exported_at=1000,
                messages=messages,
            )
            archived = _chat_payload(
                lineage="legacy_gui",
                exported_at=2000,
                messages=messages,
            )
            raw_source, _, archived_path = _write_raw_collision(root, incoming, archived)
            archived_before = archived_path.read_bytes()

            exit_code, _, stderr = _run_silently(
                [
                    "--config",
                    str(config_path),
                    "--raw-root",
                    str(raw_source),
                    "--force-overwrite",
                ]
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(archived_path.read_bytes(), archived_before)
            self.assertIn("incoming legacy 快照 exportedAt 更旧", stderr)
            self.assertIn("已有回退证据，禁止强制覆盖", stderr)

    def test_legacy_newer_exported_at_allows_shared_message_content_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            incoming = _chat_payload(
                lineage="legacy_gui",
                exported_at=2000,
                messages=[_message(1, 100, "p1", content="updated")],
            )
            archived = _chat_payload(
                lineage="legacy_gui",
                exported_at=1000,
                messages=[_message(1, 100, "p1", content="original")],
            )
            raw_source, _, archived_path = _write_raw_collision(root, incoming, archived)

            exit_code, _, stderr = _run_silently(
                ["--config", str(config_path), "--raw-root", str(raw_source)]
            )

            self.assertEqual(exit_code, 0, stderr)
            self.assertEqual(json.loads(archived_path.read_text(encoding="utf-8")), incoming)

    def test_strict_subset_is_rejected_even_with_force_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            incoming = _chat_payload(messages=[_message(2, 200, "p2")])
            archived = _chat_payload(messages=[_message(1, 100, "p1"), _message(2, 200, "p2")])
            raw_source, _, archived_path = _write_raw_collision(root, incoming, archived)
            archived_before = archived_path.read_bytes()

            exit_code, _, stderr = _run_silently(
                [
                    "--config",
                    str(config_path),
                    "--raw-root",
                    str(raw_source),
                    "--force-overwrite",
                ]
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(archived_path.read_bytes(), archived_before)
            self.assertIn("未覆盖 archived 的全部消息身份", stderr)
            self.assertIn("禁止强制覆盖", stderr)

    def test_newer_superset_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            incoming = _chat_payload(messages=[_message(1, 100, "p1"), _message(2, 200, "p2")])
            archived = _chat_payload(messages=[_message(1, 100, "p1")])
            raw_source, _, archived_path = _write_raw_collision(root, incoming, archived)

            exit_code, _, stderr = _run_silently(
                ["--config", str(config_path), "--raw-root", str(raw_source)]
            )

            self.assertEqual(exit_code, 0, stderr)
            self.assertEqual(json.loads(archived_path.read_text(encoding="utf-8")), incoming)
            self.assertTrue(raw_source.exists())

    def test_cross_lineage_zero_intersection_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            incoming = _chat_payload(lineage="legacy_gui", messages=[_message(7, 200, "incoming-id")])
            archived = _chat_payload(lineage="http_api", messages=[_message(99, 100, "archived-id")])
            raw_source, _, archived_path = _write_raw_collision(root, incoming, archived)
            archived_before = archived_path.read_bytes()

            exit_code, _, stderr = _run_silently(
                ["--config", str(config_path), "--raw-root", str(raw_source)]
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(archived_path.read_bytes(), archived_before)
            self.assertIn("platformMessageId 零交集", stderr)

    def test_force_overwrite_can_bypass_only_unverifiable_cross_lineage_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            incoming = _chat_payload(lineage="legacy_gui", messages=[_message(7, 200, "incoming-id")])
            archived = _chat_payload(lineage="http_api", messages=[_message(99, 100, "archived-id")])
            raw_source, _, archived_path = _write_raw_collision(root, incoming, archived)

            exit_code, _, stderr = _run_silently(
                [
                    "--config",
                    str(config_path),
                    "--raw-root",
                    str(raw_source),
                    "--force-overwrite",
                ]
            )

            self.assertEqual(exit_code, 0, stderr)
            self.assertEqual(json.loads(archived_path.read_text(encoding="utf-8")), incoming)
            self.assertIn("OVERWRITE GUARD OVERRIDDEN", stderr)

    def test_cross_lineage_empty_platform_id_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            incoming = _chat_payload(lineage="legacy_gui", messages=[_message(7, 200, None)])
            archived = _chat_payload(lineage="http_api", messages=[_message(99, 100, "p1")])
            raw_source, _, _ = _write_raw_collision(root, incoming, archived)

            exit_code, _, stderr = _run_silently(
                ["--config", str(config_path), "--raw-root", str(raw_source)]
            )

            self.assertEqual(exit_code, 1)
            self.assertIn("platformMessageId 存在空值", stderr)

    def test_duplicate_platform_ids_are_compared_as_a_multiset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            incoming = _chat_payload(
                lineage="legacy_gui",
                messages=[_message(1, 100, "p1"), _message(3, 200, "p2")],
            )
            archived = _chat_payload(
                lineage="http_api",
                messages=[_message(10, 100, "p1"), _message(11, 150, "p1")],
            )
            raw_source, _, archived_path = _write_raw_collision(root, incoming, archived)
            archived_before = archived_path.read_bytes()

            exit_code, _, stderr = _run_silently(
                [
                    "--config",
                    str(config_path),
                    "--raw-root",
                    str(raw_source),
                    "--force-overwrite",
                ]
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(archived_path.read_bytes(), archived_before)
            self.assertIn("按 platformMessageId 多重集合缺 1 条", stderr)

    def test_noncanonical_collision_requires_force_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            source = root / "snapshot-processed"
            source_file = source / "私聊_A" / "2026-06-11.md"
            archived_file = root / "archived" / "processed" / "私聊_A" / source_file.name
            source_file.parent.mkdir(parents=True)
            archived_file.parent.mkdir(parents=True)
            source_file.write_text("incoming", encoding="utf-8")
            archived_file.write_text("archived", encoding="utf-8")

            rejected, _, rejected_stderr = _run_silently(
                ["--config", str(config_path), "--processed-root", str(source)]
            )
            forced, _, forced_stderr = _run_silently(
                [
                    "--config",
                    str(config_path),
                    "--processed-root",
                    str(source),
                    "--force-overwrite",
                ]
            )

            self.assertEqual(rejected, 1)
            self.assertIn("非 JSON 文件内容不同", rejected_stderr)
            self.assertEqual(forced, 0, forced_stderr)
            self.assertEqual(archived_file.read_text(encoding="utf-8"), "incoming")
            self.assertTrue(source_file.exists())

    def test_nonchat_json_with_equal_data_but_different_bytes_still_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            source = root / "snapshot-raw"
            source_file = source / "朋友圈导出_20260611.json"
            archived_file = root / "archived" / "raw" / source_file.name
            payload = {"filters": {"usernames": []}, "posts": []}
            source.mkdir(parents=True)
            archived_file.parent.mkdir(parents=True)
            source_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            archived_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            archived_before = archived_file.read_bytes()

            rejected, _, rejected_stderr = _run_silently(
                ["--config", str(config_path), "--raw-root", str(source)]
            )

            self.assertEqual(rejected, 1)
            self.assertEqual(archived_file.read_bytes(), archived_before)
            self.assertIn("非聊天 JSON 内容不同", rejected_stderr)

            forced, _, forced_stderr = _run_silently(
                [
                    "--config",
                    str(config_path),
                    "--raw-root",
                    str(source),
                    "--force-overwrite",
                ]
            )

            self.assertEqual(forced, 0, forced_stderr)
            self.assertEqual(archived_file.read_bytes(), source_file.read_bytes())

    def test_batch_preflight_rejects_everything_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            raw_source = root / "snapshot-raw"
            raw_file = raw_source / "私聊_A_20260611" / "私聊_A_20260611.json"
            _write_json(raw_file, _chat_payload())
            processed_source = root / "snapshot-processed"
            processed_file = processed_source / "私聊_A" / "2026-06-11.md"
            archived_processed = root / "archived" / "processed" / "私聊_A" / processed_file.name
            processed_file.parent.mkdir(parents=True)
            archived_processed.parent.mkdir(parents=True)
            processed_file.write_text("incoming", encoding="utf-8")
            archived_processed.write_text("archived", encoding="utf-8")

            exit_code, stdout, stderr = _run_silently(
                [
                    "--config",
                    str(config_path),
                    "--raw-root",
                    str(raw_source),
                    "--processed-root",
                    str(processed_source),
                ]
            )

            self.assertEqual(exit_code, 1)
            self.assertFalse((root / "archived" / "raw" / "私聊_A" / raw_file.name).exists())
            self.assertEqual(archived_processed.read_text(encoding="utf-8"), "archived")
            self.assertNotIn("Merged", stdout)
            self.assertIn("No files were written", stderr)

    def test_guard_report_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _write_config(root)
            source = root / "snapshot-processed"
            archived = root / "archived" / "processed"
            total = MAX_REPORTED_GUARD_FAILURES + 5
            for index in range(total):
                relative = Path("私聊_A") / f"2026-06-{index + 1:02d}.md"
                source_file = source / relative
                archived_file = archived / relative
                source_file.parent.mkdir(parents=True, exist_ok=True)
                archived_file.parent.mkdir(parents=True, exist_ok=True)
                source_file.write_text(f"incoming-{index}", encoding="utf-8")
                archived_file.write_text(f"archived-{index}", encoding="utf-8")

            exit_code, _, stderr = _run_silently(
                ["--config", str(config_path), "--processed-root", str(source)]
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(stderr.count("- incoming:"), MAX_REPORTED_GUARD_FAILURES)
            self.assertIn("5 additional conflict(s) not shown", stderr)


if __name__ == "__main__":
    unittest.main()
