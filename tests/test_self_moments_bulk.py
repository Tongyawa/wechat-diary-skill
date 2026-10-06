from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import urlparse

from scripts import archive_exports, export_bulk_raw as driver
from wechat_diary_core.backends.weflow_api.backend import WeflowApiBackend
from wechat_diary_core.backends.weflow_api.client import WeflowApiClient, WeflowApiError
from wechat_diary_core.backends.weflow_api import mapper
from wechat_diary_core.config import load_config
from wechat_diary_core.raw_schema import validate_moments_json
from test_weflow_api_backend import _Client


SELF = "wxid_self_placeholder"


def _export_fixture(stage: Path) -> dict:
    # Existing backend fixture is derived from the actual /sns/export shape.
    response = _Client().export_moments(stage, [SELF])
    return json.loads(Path(response["filePath"]).read_text(encoding="utf-8"))


def _post(template: dict, identifier: str, day: date) -> dict:
    post = copy.deepcopy(template)
    timestamp = int(datetime.combine(day, datetime.min.time()).replace(hour=12).timestamp())
    post.update(id=identifier, createTime=timestamp,
                createTimeStr=datetime.fromtimestamp(timestamp).strftime("%Y/%m/%d %H:%M:%S"))
    return post


def _config(root: Path, *, self_values: list[str] | None = None, bulk: str = "") -> Path:
    path = root / "config.toml"
    path.write_text(
        '[export_backend]\nbackend = "weflow_api"\n'
        '[export_backend.weflow_api]\naccess_token = "token-placeholder"\n'
        'request_timeout_sec = 7\nmessage_request_timeout_sec = 19\n'
        '[user]\nself_wxids = ["wxid_unrelated_placeholder"]\n'
        '[daily_export]\ntarget_usernames = ["wxid_foreign_placeholder"]\n'
        f'self_moments_usernames = {json.dumps([SELF] if self_values is None else self_values)}\n'
        + bulk, encoding="utf-8",
    )
    return path


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class SelfMomentsBulkTests(unittest.TestCase):
    def test_interval_name_is_stable_and_never_collides_with_daily_even_for_one_day(self):
        day = date(2026, 8, 5)
        daily = mapper.moments_filename([SELF], day)
        interval = mapper.moments_filename([SELF], day, end=day)
        self.assertRegex(daily, r"^朋友圈导出_20260805_[0-9a-f]{8}\.json$")
        self.assertRegex(interval, r"^朋友圈导出_区间_20260805-20260805_[0-9a-f]{8}\.json$")
        self.assertNotEqual(daily, interval)
        self.assertEqual(
            mapper.moments_filename([SELF, "wxid_second_placeholder"], day, end=day),
            mapper.moments_filename(["wxid_second_placeholder", SELF], day, end=day),
        )

    def test_daily_writer_bytes_match_existing_canonical_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); stage = root / "stage"; stage.mkdir()
            source = _export_fixture(stage)
            with redirect_stderr(io.StringIO()):
                result = mapper.write_moments_export(
                    root / "raw", source, usernames=[SELF], export_date=date(2026, 8, 5), staging_dir=stage,
                )
            # Independent legacy canonical field order, encoding and no final newline.
            expected = {
                "filters": {"usernames": [SELF]},
                "posts": [{
                    "id": "post-placeholder", "username": SELF, "nickname": "示例联系人",
                    "createTime": 1785921697, "createTimeStr": "2026/08/05 12:01:37",
                    "contentDesc": "媒体未解密的示例动态", "type": 1,
                    "media": [{"url": "https://example.invalid/media/example.jpg"}],
                    "likes": [], "comments": [],
                    "location": {"poiName": "", "address": "", "cityName": ""},
                }], "totalPosts": 1, "exportTime": "2026-08-05 12:03:20",
            }
            expected_bytes = json.dumps(expected, ensure_ascii=False, indent=2).replace("\n", os.linesep).encode("utf-8")
            self.assertEqual(result.path.read_bytes(), expected_bytes)

    def test_multiyear_mapping_summary_media_and_archive_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); stage = root / "stage"; stage.mkdir()
            source = _export_fixture(stage); template = source["posts"][0]
            newer = _post(template, "post-newer", date(2026, 8, 5))
            older = _post(template, "post-older", date(2022, 12, 31))
            # Comment/like shape follows upstream src/types/sns.ts SnsPost/SnsComment.
            older["likes"] = ["Like Placeholder"]
            older["comments"] = [{"id": "comment-placeholder", "nickname": "Comment Placeholder",
                                  "content": "comment text", "refCommentId": "", "refNickname": ""}]
            older["media"].append(copy.deepcopy(older["media"][0]))
            foreign = copy.deepcopy(older); foreign["username"] = "wxid_foreign_placeholder"
            outside = _post(template, "post-outside", date(2021, 1, 1))
            source["posts"] = [newer, older, foreign, outside]
            media = stage / "media"; media.mkdir(); (media / "post-newer_1.jpg").write_bytes(b"image-placeholder")
            result = mapper.write_moments_export(
                root / "raw", source, usernames=[SELF], start=date(2022, 1, 1), end=date(2026, 8, 5), staging_dir=stage,
            )
            payload = json.loads(result.path.read_bytes())
            validate_moments_json(payload)
            self.assertEqual(result.total_posts, 2)
            self.assertEqual(result.year_counts, ((2022, 1), (2026, 1)))
            self.assertEqual(result.earliest_timestamp, older["createTime"])
            self.assertEqual(result.latest_timestamp, newer["createTime"])
            self.assertEqual(result.published_media, 1)
            self.assertEqual(result.missing_media_posts, 1)  # Two missing media in one post.
            self.assertEqual([p["id"] for p in payload["posts"]], ["post-newer", "post-older"])
            self.assertEqual(payload["posts"][1]["likes"], older["likes"])
            self.assertEqual(payload["posts"][1]["comments"], older["comments"])
            reference = payload["posts"][0]["media"][0]["localPath"]
            self.assertTrue(reference.startswith("_media/images/"))
            self.assertTrue((result.path.parent / reference).is_file())
            archive = root / "archive"
            self.assertEqual(archive_exports._find_guard_failures([(result.path, archive / result.path.name)]), [])
            # Real archive CLI against synthetic roots, never a real archive.
            config = _config(root)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = archive_exports.main(["--raw-root", str(root / "raw"), "--config", str(config)])
            self.assertEqual(code, 0)
            cfg = load_config(config)
            archived = cfg.paths.archived / "raw" / result.path.name
            self.assertEqual(archived.read_bytes(), result.path.read_bytes())
            self.assertTrue((archived.parent / reference).is_file())

    def test_media_publishes_before_json_and_failed_publish_preserves_old_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); stage = root / "stage"; stage.mkdir()
            source = _export_fixture(stage)
            media = stage / "media"; media.mkdir(); (media / "post-placeholder_1.jpg").write_bytes(b"image-placeholder")
            raw = root / "raw"; raw.mkdir()
            destination = raw / mapper.moments_filename([SELF], date(2026, 8, 5), end=date(2026, 8, 5))
            destination.write_bytes(b"previous-json-placeholder")
            replace = mapper.os.replace
            def fail_final(source_path, destination_path):
                if Path(destination_path) == destination:
                    data = json.loads(Path(source_path).read_bytes())
                    self.assertTrue((raw / data["posts"][0]["media"][0]["localPath"]).is_file())
                    raise OSError("injected final publish failure")
                return replace(source_path, destination_path)
            with patch.object(mapper.os, "replace", side_effect=fail_final):
                with self.assertRaisesRegex(OSError, "injected"):
                    mapper.write_moments_export(raw, source, usernames=[SELF], start=date(2026, 8, 5), end=date(2026, 8, 5), staging_dir=stage)
            self.assertEqual(destination.read_bytes(), b"previous-json-placeholder")

    def test_cli_runs_real_backend_with_no_chat_requests_or_state_access_and_reruns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); cfg_path = _config(root, bulk='[bulk_export]\nstart="2022-01-01"\nend="2026-08-05"\n')
            raw = root / "raw"; raw.mkdir()
            sentinels = [root / f".raw.bulk-export-{kind}.json" for kind in ("plan", "state", "reconciliation")]
            sentinels += [root / ".raw.bulk-export-state.json.journal", root / ".export-state.json"]
            for path in sentinels: path.write_bytes(b"invalid-chat-state-placeholder")
            seen = []; export_bodies = []; revision = [0]
            def opener(request, timeout):
                path = urlparse(request.full_url).path; seen.append((path, timeout))
                if path == "/health": return _Response({"status": "ok"})
                if path == "/api/v1/sessions": return _Response({"success": True, "sessions": [], "count": 0})
                self.assertEqual(path, "/api/v1/sns/export", "Unexpected endpoint, including /messages, must fail")
                body = json.loads(request.data); export_bodies.append(body)
                self.assertEqual(body["usernames"], [SELF]); self.assertTrue(body["exportMedia"])
                stage = Path(body["outputDir"]); source = _export_fixture(stage)
                source["posts"].insert(0, _post(source["posts"][0], "post-older", date(2022, 3, 4)))
                if revision[0]: source["posts"].append(_post(source["posts"][0], "post-refreshed", date(2023, 4, 5)))
                output = stage / "fixture.json"; output.write_text(json.dumps(source), encoding="utf-8")
                return _Response({"success": True, "postCount": len(source["posts"]), "filePath": str(output), "mediaCount": 0})
            backend = WeflowApiBackend(load_config(cfg_path), client_factory=lambda *a, **kw: WeflowApiClient(*a, **kw, opener=opener))
            stdout = io.StringIO(); stderr = io.StringIO()
            with ExitStack() as stack:
                stack.enter_context(patch.object(driver, "WeflowApiBackend", return_value=backend))
                for name in ("bulk_plan_path_for", "bulk_state_path_for"):
                    stack.enter_context(patch.object(driver, name, side_effect=AssertionError("chat path accessed")))
                stack.enter_context(patch.object(driver.BulkExportPlan, "load", side_effect=AssertionError("chat plan read")))
                stack.enter_context(patch.object(backend, "export_chats_range", side_effect=AssertionError("chat export called")))
                stack.enter_context(redirect_stdout(stdout)); stack.enter_context(redirect_stderr(stderr))
                argv = ["--self-moments", "--out", str(raw), "--config", str(cfg_path)]
                self.assertEqual(driver.main(argv), 0)
                first = list(raw.glob("*.json")); self.assertEqual(len(first), 1)
                revision[0] = 1
                self.assertEqual(driver.main(argv), 0)
            self.assertEqual(list(raw.glob("*.json")), first)
            self.assertEqual(json.loads(first[0].read_bytes())["totalPosts"], 3)
            self.assertIn("2022-01-01~2026-08-05", stdout.getvalue())
            self.assertIn("2022 年 1；2023 年 1；2026 年 1", stdout.getvalue())
            self.assertIn("未解密/缺失媒体的动态 3", stdout.getvalue())
            self.assertIn("最早动态：2022-03-04 12:00:00", stdout.getvalue())
            self.assertIn("[WARN]", stderr.getvalue())
            self.assertTrue(all(path != "/api/v1/messages" for path, _ in seen))
            self.assertEqual([timeout for path, timeout in seen if path == "/api/v1/sns/export"], [3600, 3600])
            for path in sentinels: self.assertEqual(path.read_bytes(), b"invalid-chat-state-placeholder")
            self.assertFalse((root / ".raw.weflow-api-moments-staging").exists())

    def test_offline_range_never_reads_plan_and_cli_overrides_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); cfg = _config(root, bulk='[bulk_export]\nstart="invalid"\nend="invalid"\n')
            raw = root / "raw"; log = root / "log.txt"
            stdout = io.StringIO()
            with patch.object(driver.BulkExportPlan, "load", side_effect=AssertionError("plan read")), patch.object(driver, "WeflowApiBackend", side_effect=AssertionError("backend constructed")), redirect_stdout(stdout):
                code = driver.main(["--self-moments", "--print-range", "--start", "20220101", "--end", "20221231", "--out", str(raw), "--log", str(log), "--config", str(cfg)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue()), {"start": "2022-01-01", "end": "2022-12-31", "source": "cli", "planRange": None, "expands": False})
            self.assertFalse(raw.exists()); self.assertFalse(log.exists())

    def test_default_range_uses_2010_to_yesterday_without_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); cfg = _config(root); stdout = io.StringIO()
            with patch.object(driver.BulkExportPlan, "load", side_effect=AssertionError("plan read")), redirect_stdout(stdout):
                self.assertEqual(driver.main(["--self-moments", "--print-range", "--out", str(root / "raw"), "--config", str(cfg)]), 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["start"], "2010-01-01")
            self.assertEqual(payload["end"], (date.today() - timedelta(days=1)).isoformat())

    def test_empty_self_configuration_fails_before_backend_or_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); cfg = _config(root, self_values=[]); stderr = io.StringIO()
            with patch.object(driver, "WeflowApiBackend", side_effect=AssertionError("backend constructed")), redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                self.assertEqual(driver.main(["--self-moments", "--out", str(root / "raw"), "--config", str(cfg)]), 1)
            self.assertIn("self_moments_usernames", stderr.getvalue())
            self.assertFalse((root / "raw").exists())

    def test_chat_control_flags_and_partial_dates_are_rejected(self):
        for flags in (["--session", "wxid_foreign_placeholder"], ["--sess", "wxid_foreign_placeholder"], ["--force"], ["--for"], ["--replan"], ["--expand-range"], ["--retry-no-local-records"], ["--chunk-target", "10000"], ["--chunk=10000"], ["--start", "20220101"]):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as captured:
                    driver.main(["--self-moments", "--out", "unused-placeholder", *flags])
                self.assertEqual(captured.exception.code, 2)

    def test_empty_interval_replaces_stale_result_but_daily_still_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); backend = WeflowApiBackend(load_config(_config(root)))
            class EmptyClient:
                def export_moments(self, *a, **kw):
                    return {"success": True, "filePath": "", "postCount": 0, "mediaCount": 0}
            backend._client = EmptyClient()
            result = backend.export_moments_range([SELF], date(2022, 1, 1), date(2026, 8, 5), raw_root=root / "raw")
            result.path.write_bytes(b"stale-placeholder")
            rerun = backend.export_moments_range([SELF], date(2022, 1, 1), date(2026, 8, 5), raw_root=root / "raw")
            self.assertEqual(rerun.total_posts, 0); self.assertEqual(rerun.year_counts, ())
            self.assertIsNone(rerun.earliest_timestamp); self.assertIsNone(rerun.latest_timestamp)
            validate_moments_json(json.loads(rerun.path.read_bytes()))
            self.assertEqual(backend.export_moments([SELF], date(2026, 8, 5)), None)
            self.assertEqual(list((root / "raw").glob("朋友圈导出_20260805_*.json")), [])

    def test_moments_timeout_is_separate_from_control_messages_and_timeline(self):
        seen = []
        def opener(request, timeout):
            path = urlparse(request.full_url).path; seen.append((path, timeout))
            if path == "/health": return _Response({"status": "ok"})
            if path.endswith("timeline"): return _Response({"timeline": [], "count": 0})
            if path.endswith("messages"): return _Response({"messages": [], "hasMore": False, "count": 0})
            return _Response({"success": True, "postCount": 0, "filePath": ""})
        client = WeflowApiClient("http://example.invalid", "token-placeholder", timeout=7, message_timeout=19, moments_timeout=3701, opener=opener)
        client.health(); list(client.iter_timeline(SELF))
        client.fetch_messages(SELF, start=date(2022, 1, 1), end=date(2022, 1, 1))
        client.export_moments("stage-placeholder", [SELF], start=date(2022, 1, 1), end=date(2026, 1, 1))
        self.assertEqual(seen, [("/health", 7), ("/api/v1/sns/timeline", 7), ("/api/v1/messages", 19), ("/api/v1/sns/export", 3701)])

    def test_moments_timeout_reports_own_key_for_direct_and_wrapped_timeouts(self):
        for error in (TimeoutError("placeholder"), URLError(TimeoutError("placeholder"))):
            with self.subTest(error=type(error).__name__):
                def opener(request, timeout):
                    self.assertEqual(timeout, 3701)
                    raise error
                client = WeflowApiClient("http://example.invalid", "token-placeholder", timeout=7, moments_timeout=3701, opener=opener)
                with self.assertRaises(WeflowApiError) as captured:
                    client.export_moments("stage-placeholder", [SELF], start=date(2022, 1, 1), end=date(2026, 1, 1))
                text = str(captured.exception)
                self.assertIn("朋友圈数据超时", text); self.assertIn("3701 秒", text)
                self.assertIn(".moments_request_timeout_sec", text); self.assertIn("原命令重跑", text)
                self.assertNotIn("控制面超时", text)

    def test_new_timeout_default_and_explicit_override_reach_backend_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); path = _config(root)
            self.assertEqual(load_config(path).export_backend.weflow_api.moments_request_timeout_sec, 3600)
            text = path.read_text(encoding="utf-8").replace("request_timeout_sec = 7", "request_timeout_sec = 7\nmoments_request_timeout_sec = 4701")
            path.write_text(text, encoding="utf-8")
            backend = WeflowApiBackend(load_config(path))
            self.assertEqual(backend.client.timeout, 7)
            self.assertEqual(backend.client.message_timeout, 19)
            self.assertEqual(backend.client.moments_timeout, 4701)


if __name__ == "__main__":
    unittest.main()
