from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from wechat_diary_core import on_demand_image_backfill as bridge


def machine_report(total=1, patched=1, **reasons):
    return {
        "schema_version": 1, "mode": "single", "status": "completed", "applied": True,
        "degraded_images": total, "patched_messages": patched,
        **dict.fromkeys(bridge.REASONS, 0), **reasons,
    }


def prepare(root, count=1):
    session = root / "staging" / "session"
    session.mkdir(parents=True)
    path = session / "chat.json"
    path.write_text(json.dumps({
        "weflow": {"source": "http_api"}, "session": {"wxid": "wxid_synthetic_session"},
        "messages": [{"type": "图片消息", "source": "", "content": "[图片]", "localId": i} for i in range(count)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    script = root / "repair.py"
    script.write_text("# synthetic script", encoding="utf-8")
    config = root / "custom.toml"
    config.write_text("", encoding="utf-8")
    return session, path, script, config


def fill(command, report=None):
    session = Path(command[command.index("--canonical-dir") + 1])
    path = session / "chat.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    target = session.parent / "_media/images/aa/aaaaaaaa.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"synthetic-media")
    for message in payload["messages"]:
        if message.get("type") == "图片消息" and message.get("source") == "":
            message["source"] = "../_media/images/aa/aaaaaaaa.jpg"
            message["content"] = message["source"]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    output = Path(command[command.index("--report-json") + 1])
    output.write_text(json.dumps(report or machine_report()), encoding="utf-8")
    return 0


class OnDemandImageBackfillTests(unittest.TestCase):
    def test_apply_counts_messages_not_deduplicated_cas_and_uses_exact_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, path, script, config = prepare(Path(tmp), count=2)
            def runner(command, **kwargs):
                self.assertEqual(sys.executable, command[0])
                self.assertEqual(str(config), command[command.index("--config") + 1])
                self.assertNotIn("--raw-root", command)
                self.assertGreater(kwargs["timeout"], 180)
                return fill(command, machine_report(2, 2))
            with mock.patch.object(bridge, "_run_script", side_effect=runner):
                result = bridge.backfill_on_demand(session, script=script, config_path=config, require_media=True)
            self.assertEqual((2, 0, "completed"), (result.patched_messages, result.remaining, result.status))
            self.assertEqual(1, len(list((session.parent / "_media").rglob("*.jpg"))))
            self.assertTrue(all(m["source"] for m in json.loads(path.read_text(encoding="utf-8"))["messages"]))

    def test_report_negative_controls_reject_bad_values_and_bad_shapes(self):
        good = machine_report(7, 2, file_missing=1, unsupported_format=1, stem_unresolved=1, image_incomplete=1, ffmpeg_unavailable=1)
        self.assertEqual(2, bridge._checked_report(good, 7, 2)["patched_messages"])
        bad = dict(good, file_missing=8)
        with self.assertRaises(ValueError):
            bridge._checked_report(bad, 7, 2)
        for shaped in ([good], {"schema_version": 1}, dict(good, applied=False), dict(good, patched_messages=True)):
            with self.subTest(shaped=shaped), self.assertRaises(ValueError):
                bridge._checked_report(shaped, 7, 2)

    def test_partial_result_keeps_all_five_reasons_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, path, script, config = prepare(Path(tmp), count=7)
            def runner(command, **kwargs):
                fill(command)
                payload = json.loads(path.read_text(encoding="utf-8"))
                for item in payload["messages"][2:]:
                    item.update(source="", content="[图片]")
                path.write_text(json.dumps(payload), encoding="utf-8")
                report = machine_report(7, 2, **dict.fromkeys(bridge.REASONS, 1))
                Path(command[-1]).write_text(json.dumps(report), encoding="utf-8")
                return 0
            with mock.patch.object(bridge, "_run_script", side_effect=runner):
                result = bridge.backfill_on_demand(session, script=script, config_path=config, require_media=True)
            self.assertEqual((2, 5, 0), (result.patched_messages, result.remaining, result.unchecked))
            self.assertEqual([1] * 5, [getattr(result, key) for key in bridge.REASONS])

    def test_failed_or_malformed_tool_restores_original_bytes(self):
        for failure in ("nonzero", "missing_report", "bad_counts", "dangling_source", "existing_source", "added_json", "timeout"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                session, path, script, config = prepare(Path(tmp))
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["messages"].append({"type": "图片消息", "source": "../_media/images/old.jpg", "content": "existing"})
                path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                original = path.read_bytes()
                def runner(command, **kwargs):
                    fill(command)
                    report_path = Path(command[-1])
                    if failure == "nonzero":
                        return 1
                    if failure == "missing_report":
                        report_path.unlink()
                    elif failure == "bad_counts":
                        report_path.write_text(json.dumps(machine_report(1, 5)), encoding="utf-8")
                    elif failure == "dangling_source":
                        next((session.parent / "_media").rglob("*.jpg")).unlink()
                    elif failure == "existing_source":
                        changed = json.loads(path.read_text(encoding="utf-8"))
                        changed["messages"][1]["source"] = "wrong.jpg"
                        path.write_text(json.dumps(changed), encoding="utf-8")
                    elif failure == "added_json":
                        (session / "extra.json").write_bytes(path.read_bytes())
                    elif failure == "timeout":
                        raise subprocess.TimeoutExpired(command, 1)
                    return 0
                with mock.patch.object(bridge, "_run_script", side_effect=runner):
                    result = bridge.backfill_on_demand(session, script=script, config_path=config, require_media=True)
                self.assertEqual(original, path.read_bytes())
                self.assertEqual((0, 1, 0), (result.patched_messages, result.unchecked, result.file_missing))
                self.assertEqual([path], list(session.glob("*.json")))
                self.assertEqual([], list((session.parent / "_media").rglob("*.jpg")))

    def test_restore_failure_propagates_instead_of_claiming_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, path, script, config = prepare(Path(tmp))
            with mock.patch.object(bridge, "_run_script", return_value=1), mock.patch.object(bridge, "_restore", side_effect=OSError("synthetic disk failure")):
                with self.assertRaises(OSError):
                    bridge.backfill_on_demand(session, script=script, config_path=config, require_media=True)

    def test_missing_tool_and_disabled_media_never_call_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, path, script, config = prepare(Path(tmp))
            before = path.read_bytes()
            with mock.patch.object(bridge, "_run_script", side_effect=AssertionError("must not launch")):
                for tool, required, expected in ((None, True, "not_configured"), (script.with_name("missing.py"), True, "unavailable"), (script, False, "media_disabled")):
                    result = bridge.backfill_on_demand(session, script=tool, config_path=config, require_media=required)
                    self.assertEqual(expected, result.status)
                payload = json.loads(before)
                payload["messages"][0]["source"] = "../_media/images/existing.jpg"
                path.write_text(json.dumps(payload), encoding="utf-8")
                existing = path.read_bytes()
                result = bridge.backfill_on_demand(session, script=script, config_path=config, require_media=True)
                self.assertEqual("not_needed", result.status)
                self.assertEqual(existing, path.read_bytes())

    def test_warning_uses_final_raw_and_quotes_paths_without_fake_command_when_disabled(self):
        result = bridge.ImageBackfillResult(degraded_images=3, unchecked=3, status="failed")
        errors = io.StringIO()
        with redirect_stderr(errors):
            bridge.print_backfill_result(result, script=Path("repair's file.py"), config_path=Path("custom config.toml"), raw_session_dir=Path("final raw/session"), process_script=Path("process_existing_raw.py"))
        text = errors.getvalue()
        self.assertIn("repair''s file.py", text)
        self.assertIn("'final raw/session'".replace("/", os.sep), text)
        self.assertIn("未检查 3", text)
        self.assertIn("字节可能仍在本地", text)
        self.assertIn("process_existing_raw.py", text)
        errors = io.StringIO()
        with redirect_stderr(errors):
            bridge.print_backfill_result(result, script=None, config_path=Path("custom.toml"), raw_session_dir=Path("final"), process_script=Path("process_existing_raw.py"))
        self.assertIn("[image_backfill].script", errors.getvalue())
        self.assertNotIn("--apply", errors.getvalue())

    def test_unavailable_script_explains_missing_path_without_unusable_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, path, script, config = prepare(Path(tmp), count=2)
            script.unlink()
            with mock.patch.object(bridge, "_run_script", side_effect=AssertionError("missing script must not run")):
                unavailable = bridge.backfill_on_demand(session, script=script, config_path=config, require_media=True)
            self.assertEqual("unavailable", unavailable.status)
            def warning(result):
                errors = io.StringIO()
                with redirect_stderr(errors):
                    bridge.print_backfill_result(result, script=script, config_path=config, raw_session_dir=Path("final raw/session"), process_script=Path("process_existing_raw.py"))
                return errors.getvalue()
            text = warning(unavailable)
            self.assertIn(f"配置的补数脚本不存在：{script}", text)
            self.assertIn("请修正 config.toml 的 [image_backfill].script", text)
            self.assertNotIn("手工补数", text)
            self.assertNotIn("--apply", text)
            self.assertNotIn("--canonical-dir", text)
            # Different filesystem shape / result value: an existing script's
            # failure must still offer the current manual recovery command.
            script.write_text("# synthetic available script", encoding="utf-8")
            with mock.patch.object(bridge, "_run_script", return_value=1):
                failed = bridge.backfill_on_demand(session, script=script, config_path=config, require_media=True)
            self.assertEqual("failed", failed.status)
            self.assertIn("手工补数", warning(failed))
            self.assertIn("--apply", warning(failed))
            self.assertNotIn("配置的补数脚本不存在", warning(failed))

    def test_real_subprocess_success_and_timeout_kill_descendant_before_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "writer.py"
            output = root / "output.txt"
            script.write_text("import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('done')", encoding="utf-8")
            self.assertEqual(0, bridge._run_script([sys.executable, str(script), str(output)], timeout=5))
            self.assertEqual("done", output.read_text())
            # Child writes repeatedly: merely killing the parent would leave it alive.
            marker = root / "child.txt"
            ready = root / "ready.txt"
            child_code = "import pathlib,time; p=pathlib.Path(" + repr(str(marker)) + "); " + "exec('while True:\\n p.write_text(str(time.time()))\\n time.sleep(0.02)')"
            script.write_text(
                "import pathlib,subprocess,sys,time\n"
                + "subprocess.Popen([sys.executable,'-c'," + repr(child_code) + "])\n"
                + "pathlib.Path(" + repr(str(ready)) + ").write_text('ready')\n"
                + "time.sleep(60)\n", encoding="utf-8",
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                bridge._run_script([sys.executable, str(script)], timeout=1)
            self.assertTrue(ready.exists())
            self.assertTrue(marker.exists(), "child control must actually write")
            stopped = marker.read_bytes()
            time.sleep(0.15)
            self.assertEqual(stopped, marker.read_bytes())
            time.sleep(0.3)
            self.assertEqual(stopped, marker.read_bytes())


if __name__ == "__main__":
    unittest.main()
