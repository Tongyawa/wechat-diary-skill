from __future__ import annotations

import json
import io
import unittest
from datetime import date
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from wechat_diary_core.backends.weflow_api.client import WeflowApiClient, WeflowApiError


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class WeflowApiClientTests(unittest.TestCase):
    def test_http_500_includes_weflow_error_reason(self) -> None:
        reason = "创建游标失败: -3（消息数据库未找到）"

        def opener(request, timeout):
            raise HTTPError(
                request.full_url,
                500,
                "Internal Server Error",
                hdrs=None,
                fp=io.BytesIO(json.dumps({"error": reason}, ensure_ascii=False).encode("utf-8")),
            )

        client = WeflowApiClient("http://127.0.0.1:5031", "token-placeholder", opener=opener)
        with self.assertRaisesRegex(WeflowApiError, reason) as captured:
            client.get_message_page(
                "wxid_contact_placeholder",
                start=date(2026, 8, 6),
                end=date(2026, 8, 6),
            )

        self.assertIn("WeFlow API HTTP 500: /api/v1/messages — ", str(captured.exception))

    def test_http_500_body_edge_cases_fall_back_or_truncate_safely(self) -> None:
        cases = [
            (b"not-json", False),
            (json.dumps({"message": "missing error"}).encode("utf-8"), False),
            (json.dumps({"error": "x" * 2000}).encode("utf-8"), True),
        ]
        for body, has_detail in cases:
            with self.subTest(has_detail=has_detail, body_length=len(body)):
                def opener(request, timeout, response_body=body):
                    raise HTTPError(
                        request.full_url,
                        500,
                        "Internal Server Error",
                        hdrs=None,
                        fp=io.BytesIO(response_body),
                    )

                client = WeflowApiClient("http://127.0.0.1:5031", "token-placeholder", opener=opener)
                with self.assertRaises(WeflowApiError) as captured:
                    client.validate_token()
                message = str(captured.exception)

                self.assertTrue(message.startswith("WeFlow API HTTP 500: /api/v1/sessions"))
                self.assertEqual(" — " in message, has_detail)
                if has_detail:
                    self.assertTrue(message.endswith("…"))
                    self.assertLess(len(message), 600)

    def test_http_error_message_never_echoes_access_token(self) -> None:
        token = "secret-token-placeholder"

        def opener(request, timeout):
            body = json.dumps({"error": f"upstream echoed Bearer {token}"}).encode("utf-8")
            raise HTTPError(request.full_url, 500, "Internal Server Error", hdrs=None, fp=io.BytesIO(body))

        client = WeflowApiClient("http://127.0.0.1:5031", token, opener=opener)
        with self.assertRaises(WeflowApiError) as captured:
            client.validate_token()

        self.assertNotIn(token, str(captured.exception))
        self.assertIn("[REDACTED]", str(captured.exception))

    def test_message_range_paginates_and_uses_bearer_auth(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append(request)
            query = parse_qs(urlparse(request.full_url).query)
            offset = int(query["offset"][0])
            if offset == 0:
                return _Response(
                    {
                        "success": True,
                        "talker": "wxid_contact_placeholder",
                        "count": 2,
                        "hasMore": True,
                        "media": {"enabled": True, "exportPath": "C:/placeholder", "count": 0},
                        "messages": [{"localId": 2, "serverId": "server-2"}],
                    }
                )
            return _Response(
                {
                    "success": True,
                    "talker": "wxid_contact_placeholder",
                    "count": 2,
                    "hasMore": False,
                    "media": {"enabled": True, "exportPath": "C:/placeholder", "count": 0},
                    "messages": [{"localId": 1, "serverId": "server-1"}],
                }
            )

        client = WeflowApiClient("http://127.0.0.1:5031", "fixed-token", opener=opener)
        messages = client.fetch_messages(
            "wxid_contact_placeholder",
            start=date(2026, 7, 1),
            end=date(2026, 8, 5),
            limit=1,
        )

        self.assertEqual([item["serverId"] for item in messages], ["server-2", "server-1"])
        first_query = parse_qs(urlparse(requests[0].full_url).query)
        self.assertEqual(first_query["start"], ["20260701"])
        self.assertEqual(first_query["end"], ["20260805"])
        self.assertEqual(first_query["format"], ["json"])
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer fixed-token")

    def test_timeline_always_sends_usernames_filters_locally_and_exhausts_offsets(self) -> None:
        queries = []

        def opener(request, timeout):
            query = parse_qs(urlparse(request.full_url).query)
            queries.append(query)
            if query["offset"] == ["0"]:
                return _Response(
                    {
                        "success": True,
                        "count": 3,
                        "timeline": [
                            {"tid": "1", "username": "wxid_contact_placeholder"},
                            {"tid": "foreign", "username": "wxid_foreign_placeholder"},
                        ],
                    }
                )
            return _Response(
                {
                    "success": True,
                    "count": 3,
                    "timeline": [{"tid": "2", "username": "wxid_contact_placeholder"}],
                }
            )

        client = WeflowApiClient("http://127.0.0.1:5031", "fixed-token", opener=opener)
        posts = list(client.iter_timeline("wxid_contact_placeholder", limit=2))

        self.assertEqual([post["tid"] for post in posts], ["1", "2"])
        self.assertEqual([query["offset"] for query in queries], [["0"], ["2"]])
        self.assertTrue(all(query["usernames"] == ["wxid_contact_placeholder"] for query in queries))
        self.assertTrue(all("start" not in query and "end" not in query for query in queries))

    def test_export_moments_posts_exact_media_request_to_staging(self) -> None:
        captured = []

        def opener(request, timeout):
            captured.append(request)
            return _Response(
                {
                    "success": True,
                    "filePath": "C:/staging/朋友圈导出_2026-08-05T12-03-20.json",
                    "postCount": 2,
                    "mediaCount": 19,
                }
            )

        client = WeflowApiClient("http://127.0.0.1:5031", "fixed-token", opener=opener)
        response = client.export_moments(
            "C:/staging",
            ["wxid_contact_placeholder"],
            start=date(2026, 8, 1),
            end=date(2026, 8, 5),
        )

        request = captured[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.method, "POST")
        self.assertEqual(urlparse(request.full_url).path, "/api/v1/sns/export")
        self.assertEqual(body["usernames"], ["wxid_contact_placeholder"])
        self.assertEqual(body["start"], "20260801")
        self.assertEqual(body["end"], "20260805")
        self.assertTrue(Path(body["outputDir"]).is_absolute())
        self.assertIs(body["exportMedia"], True)
        self.assertNotIn("media", body)
        self.assertNotIn("withMedia", body)
        self.assertEqual(response["mediaCount"], 19)

    def test_export_moments_rejects_empty_targets_before_request(self) -> None:
        client = WeflowApiClient(
            "http://127.0.0.1:5031",
            "fixed-token",
            opener=lambda *args, **kwargs: self.fail("empty usernames must not reach API"),
        )
        with self.assertRaisesRegex(ValueError, "不能为空"):
            client.export_moments("C:/staging", [], start=date(2026, 8, 5), end=date(2026, 8, 5))

    def test_export_moments_accepts_successful_empty_result_without_file(self) -> None:
        expected = {"success": True, "filePath": "", "postCount": 0, "mediaCount": 0}
        client = WeflowApiClient(
            "http://127.0.0.1:5031",
            "fixed-token",
            opener=lambda request, timeout: _Response(expected),
        )

        response = client.export_moments(
            "C:/staging",
            ["wxid_contact_placeholder"],
            start=date(2026, 8, 6),
            end=date(2026, 8, 6),
        )

        self.assertEqual(response, expected)

    def test_export_moments_still_rejects_unsuccessful_result(self) -> None:
        client = WeflowApiClient(
            "http://127.0.0.1:5031",
            "fixed-token",
            opener=lambda request, timeout: _Response({"success": False, "error": "fixture failure"}),
        )

        with self.assertRaisesRegex(WeflowApiError, "fixture failure"):
            client.export_moments(
                "C:/staging",
                ["wxid_contact_placeholder"],
                start=date(2026, 8, 6),
                end=date(2026, 8, 6),
            )

    def test_contacts_explicit_large_limit_rejects_exactly_100_truncation_signal(self) -> None:
        captured = []

        def opener(request, timeout):
            captured.append(parse_qs(urlparse(request.full_url).query))
            return _Response(
                {
                    "success": True,
                    "count": 100,
                    "contacts": [{"username": f"wxid_{index}"} for index in range(100)],
                }
            )

        client = WeflowApiClient("http://127.0.0.1:5031", "fixed-token", opener=opener)
        with self.assertRaisesRegex(WeflowApiError, "恰好 100"):
            client.fetch_contacts()

        self.assertEqual(captured[0]["limit"], ["5000"])

    def test_health_is_unauthenticated_but_token_probe_is_protected(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append(request)
            if urlparse(request.full_url).path == "/health":
                return _Response({"status": "ok"})
            return _Response({"success": True, "count": 0, "sessions": []})

        client = WeflowApiClient("http://127.0.0.1:5031", "fixed-token", opener=opener)
        client.health()
        client.validate_token()

        self.assertIsNone(requests[0].get_header("Authorization"))
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer fixed-token")


if __name__ == "__main__":
    unittest.main()
