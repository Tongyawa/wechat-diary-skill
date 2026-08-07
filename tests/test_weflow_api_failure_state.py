from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from wechat_diary_core.backends.weflow_api.failure_state import (
    SessionFailureState,
    error_fingerprint,
    normalize_error_text,
)


class SessionFailureStateTests(unittest.TestCase):
    def test_error_fingerprint_ignores_variable_parts_but_preserves_failure_type(self) -> None:
        first = (
            "WeFlow API HTTP 500: /api/v1/messages — 创建游标失败: -3 "
            "wxid_first_placeholder C:\\Users\\One\\data\\shard_123.db"
        )
        same_type = (
            "WeFlow API HTTP 503: /api/v2/messages — 创建游标失败: -99 "
            "wxid_second_placeholder D:\\Backup\\data\\shard_987.db"
        )
        different_type = "WeFlow API HTTP 500: /api/v1/messages — 鉴权失败: token 已过期"

        self.assertEqual(error_fingerprint(first), error_fingerprint(same_type))
        self.assertNotEqual(error_fingerprint(first), error_fingerprint(different_type))
        normalized = normalize_error_text(first)
        self.assertIn("<path>", normalized)
        self.assertIn("<id>", normalized)
        self.assertIn("<num>", normalized)

    def test_failure_count_uses_distinct_export_dates_and_reviews_on_third_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".export-state.json"
            state = SessionFailureState(path)

            state.record_failure(
                "wxid_failed_placeholder",
                "失败会话占位",
                date(2026, 8, 4),
                "fixture cursor failure",
            )
            state.record_failure(
                "wxid_failed_placeholder",
                "失败会话占位",
                date(2026, 8, 4),
                "fixture cursor failure",
            )
            self.assertEqual(state.failures["wxid_failed_placeholder"]["consecutiveFailures"], 1)
            self.assertEqual(state.pending_review(), [])

            state.record_failure(
                "wxid_failed_placeholder",
                "失败会话占位",
                date(2026, 8, 5),
                "fixture cursor failure",
            )
            self.assertEqual(state.failures["wxid_failed_placeholder"]["consecutiveFailures"], 2)
            self.assertEqual(state.pending_review(), [])

            state.record_failure(
                "wxid_failed_placeholder",
                "失败会话占位",
                date(2026, 8, 6),
                "fixture cursor failure",
            )
            state.save()
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["failures"]["wxid_failed_placeholder"]["consecutiveFailures"], 3)
        self.assertEqual(
            payload["failures"]["wxid_failed_placeholder"]["failureDates"],
            ["2026-08-04", "2026-08-05", "2026-08-06"],
        )
        self.assertEqual(len(state.pending_review()), 1)

    def test_success_clears_failure_and_ignored_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".export-state.json"
            state = SessionFailureState(path)
            for day in (4, 5, 6):
                state.record_failure(
                    "wxid_recovered_placeholder",
                    "恢复会话占位",
                    date(2026, 8, day),
                    "fixture cursor failure",
                )
            state.ignore(["wxid_recovered_placeholder"], authorized_date=date(2026, 8, 6))

            was_ignored = state.record_success("wxid_recovered_placeholder")
            state.save()
            restored = SessionFailureState.load(path)

        self.assertTrue(was_ignored)
        self.assertEqual(restored.failures, {})
        self.assertEqual(restored.ignored, {})


if __name__ == "__main__":
    unittest.main()
