from __future__ import annotations

import json
import io
import socket
import unittest
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
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
    def test_control_plane_timeout_guides_api_restart_without_date_advice(self) -> None:
        def opener(request, timeout):
            raise TimeoutError("timed out")

        client = WeflowApiClient("http://127.0.0.1:5031", "token-placeholder", timeout=42, opener=opener)
        with self.assertRaises(WeflowApiError) as captured:
            client.validate_token()

        message = str(captured.exception)
        self.assertIn("控制面超时", message)
        self.assertIn("42 秒", message)
        self.assertIn("重启 WeFlow API 服务", message)
        self.assertNotIn("缩小日期范围", message)
        self.assertIn("request_timeout_sec", message)

    def test_message_data_timeout_uses_long_limit_and_date_range_advice(self) -> None:
        def opener(request, timeout):
            self.assertEqual(timeout, 642)
            raise TimeoutError("timed out")

        client = WeflowApiClient(
            "http://127.0.0.1:5031",
            "token-placeholder",
            timeout=42,
            message_timeout=642,
            opener=opener,
        )
        with self.assertRaises(WeflowApiError) as captured:
            client.fetch_messages(
                "wxid_contact_placeholder",
                start=date(2026, 8, 6),
                end=date(2026, 8, 6),
            )

        message = str(captured.exception)
        self.assertIn("消息数据超时", message)
        self.assertIn("642 秒", message)
        self.assertIn("缩小日期范围", message)
        self.assertIn("message_request_timeout_sec", message)

    def test_control_and_message_requests_apply_separate_timeouts(self) -> None:
        seen = []

        def opener(request, timeout):
            path = urlparse(request.full_url).path
            seen.append((path, timeout))
            if path == "/api/v1/sessions":
                return _Response({"success": True, "count": 0, "sessions": []})
            return _Response({"success": True, "hasMore": False, "messages": []})

        client = WeflowApiClient(
            "http://127.0.0.1:5031",
            "token-placeholder",
            timeout=17,
            message_timeout=617,
            opener=opener,
        )
        client.validate_token()
        client.fetch_messages(
            "wxid_contact_placeholder",
            start=date(2026, 8, 6),
            end=date(2026, 8, 6),
        )

        self.assertEqual(seen, [("/api/v1/sessions", 17), ("/api/v1/messages", 617)])

    def test_non_timeout_connection_error_keeps_connection_message(self) -> None:
        def opener(request, timeout):
            raise URLError("connection refused")

        client = WeflowApiClient("http://127.0.0.1:5031", "token-placeholder", opener=opener)
        with self.assertRaisesRegex(WeflowApiError, "无法连接 WeFlow API") as captured:
            client.validate_token()

        self.assertNotIn("超时而非服务不可达", str(captured.exception))

    def test_url_error_wrapping_socket_timeout_uses_timeout_message(self) -> None:
        def opener(request, timeout):
            raise URLError(socket.timeout("timed out"))

        client = WeflowApiClient("http://127.0.0.1:5031", "token-placeholder", timeout=9, opener=opener)
        with self.assertRaisesRegex(WeflowApiError, "控制面超时"):
            client.validate_token()

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
                        # Real /messages responses declare this page's size,
                        # not the total number of matching messages.
                        "count": 1,
                        "hasMore": True,
                        "media": {"enabled": True, "exportPath": "C:/placeholder", "count": 0},
                        "messages": [{"localId": 2, "serverId": "server-2"}],
                    }
                )
            return _Response(
                {
                    "success": True,
                    "talker": "wxid_contact_placeholder",
                    "count": 1,
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

    def test_single_day_message_page_still_returns_unchanged_in_one_request(self) -> None:
        requests = []
        expected = [
            {"localId": index, "serverId": f"server-{index}"}
            for index in range(5, 0, -1)
        ]

        def opener(request, timeout):
            requests.append(request)
            return _Response(
                {
                    "success": True,
                    "count": 5,
                    "hasMore": False,
                    "messages": expected,
                }
            )

        client = WeflowApiClient("http://127.0.0.1:5031", "fixed-token", opener=opener)
        actual = client.fetch_messages(
            "wxid_contact_placeholder",
            start=date(2026, 8, 20),
            end=date(2026, 8, 20),
            media=False,
        )

        self.assertEqual(actual, expected)
        self.assertEqual(len(requests), 1)
        query = parse_qs(urlparse(requests[0].full_url).query)
        self.assertEqual(query["start"], ["20260820"])
        self.assertEqual(query["end"], ["20260820"])
        self.assertNotIn("media", query)

    def test_timeline_always_sends_usernames_filters_locally_and_exhausts_offsets(self) -> None:
        queries = []

        def opener(request, timeout):
            query = parse_qs(urlparse(request.full_url).query)
            queries.append(query)
            if query["offset"] == ["0"]:
                return _Response(
                    {
                        "success": True,
                        # httpService.ts emits count: timeline.length.
                        "count": 2,
                        "timeline": [
                            {"tid": "1", "username": "wxid_contact_placeholder"},
                            {"tid": "foreign", "username": "wxid_foreign_placeholder"},
                        ],
                    }
                )
            return _Response(
                {
                    "success": True,
                    "count": 1,
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

    def test_contacts_page_below_limit_is_complete_when_count_matches_page_size(self) -> None:
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
        contacts = client.fetch_contacts()

        self.assertEqual(captured[0]["limit"], ["10000"])
        self.assertEqual(len(contacts), 100)

    def test_sessions_page_below_limit_is_complete_without_has_more(self) -> None:
        offsets = []

        def opener(request, timeout):
            offset = int(parse_qs(urlparse(request.full_url).query)["offset"][0])
            offsets.append(offset)
            return _Response(
                {
                    "success": True,
                    "count": 2,
                    "sessions": [{"username": "session-1"}, {"username": "session-2"}],
                }
            )

        client = WeflowApiClient("http://127.0.0.1:5031", "fixed-token", opener=opener)
        sessions = client.fetch_sessions(limit=5000)

        self.assertEqual([item["username"] for item in sessions], ["session-1", "session-2"])
        self.assertEqual(offsets, [0])

    def test_bounded_collection_rejects_full_page_as_known_truncation_risk(self) -> None:
        for endpoint, field, fetch in (
            ("/api/v1/sessions", "sessions", lambda client: client.fetch_sessions(limit=2)),
            ("/api/v1/contacts", "contacts", lambda client: client.fetch_contacts(limit=2)),
        ):
            with self.subTest(endpoint=endpoint):
                def opener(request, timeout, page_field=field):
                    return _Response(
                        {
                            "success": True,
                            # Real sessions/contacts count is the returned page size.
                            "count": 2,
                            page_field: [{"username": "item-1"}, {"username": "item-2"}],
                        }
                    )

                client = WeflowApiClient("http://127.0.0.1:5031", "fixed-token", opener=opener)
                with self.assertRaises(WeflowApiError) as captured:
                    fetch(client)

                error = str(captured.exception)
                self.assertIn("触及 limit=2", error)
                self.assertIn("无法证明结果完整", error)
                self.assertLess(len(error), 240)

    def test_contacts_page_count_mismatch_fails_loudly(self) -> None:
        def opener(request, timeout):
            return _Response(
                {"success": True, "count": 2, "contacts": [{"username": "contact-1"}]}
            )

        client = WeflowApiClient("http://127.0.0.1:5031", "fixed-token", opener=opener)
        with self.assertRaises(WeflowApiError) as captured:
            client.fetch_contacts()

        error = str(captured.exception)
        self.assertIn("本页实际返回 1 条", error)
        self.assertIn("count=2", error)
        self.assertIn("检查 API 分页", error)
        self.assertLess(len(error), 240)

    def test_messages_without_has_more_fail_instead_of_guessing_from_page_length(self) -> None:
        def opener(request, timeout):
            return _Response(
                {
                    "success": True,
                    "count": 1,
                    "messages": [{"localId": 3, "serverId": "server-3"}],
                }
            )

        client = WeflowApiClient("http://127.0.0.1:5031", "fixed-token", opener=opener)
        with self.assertRaisesRegex(WeflowApiError, r"缺少 hasMore.*无法证明消息已取全"):
            client.fetch_messages(
                "wxid_contact_placeholder",
                start=date(2026, 8, 5),
                end=date(2026, 8, 5),
            )

    def test_message_page_count_mismatch_fails_loudly(self) -> None:
        def opener(request, timeout):
            return _Response(
                {
                    "success": True,
                    "count": 2,
                    "hasMore": False,
                    "messages": [{"localId": 1, "serverId": "server-1"}],
                }
            )

        client = WeflowApiClient("http://127.0.0.1:5031", "fixed-token", opener=opener)
        with self.assertRaisesRegex(WeflowApiError, r"count=2.*本页实际返回 1 条"):
            client.fetch_messages(
                "wxid_contact_placeholder",
                start=date(2026, 8, 5),
                end=date(2026, 8, 5),
            )

    def test_messages_repeated_page_fails_instead_of_looping(self) -> None:
        def opener(request, timeout):
            return _Response(
                {
                    "success": True,
                    "hasMore": True,
                    "messages": [{"localId": 1, "serverId": "server-1"}],
                }
            )

        client = WeflowApiClient("http://127.0.0.1:5031", "fixed-token", opener=opener)
        with self.assertRaisesRegex(WeflowApiError, r"重复消息 ID.*死循环"):
            client.fetch_messages(
                "wxid_contact_placeholder",
                start=date(2026, 8, 5),
                end=date(2026, 8, 5),
            )

    def test_timeline_repeated_full_page_fails_instead_of_silently_deduplicating(self) -> None:
        def opener(request, timeout):
            return _Response(
                {
                    "success": True,
                    "count": 1,
                    "timeline": [{"tid": "same-post", "username": "wxid_contact_placeholder"}],
                }
            )

        client = WeflowApiClient("http://127.0.0.1:5031", "fixed-token", opener=opener)
        with self.assertRaisesRegex(WeflowApiError, r"重复 ID.*静默漏取"):
            list(client.iter_timeline("wxid_contact_placeholder", limit=1))

    def test_group_members_count_mismatch_fails_loudly(self) -> None:
        def opener(request, timeout):
            return _Response(
                {
                    "success": True,
                    "count": 2,
                    "members": [{"wxid": "member-1"}],
                }
            )

        client = WeflowApiClient("http://127.0.0.1:5031", "fixed-token", opener=opener)
        with self.assertRaisesRegex(WeflowApiError, r"count=2.*本页实际返回 1 条"):
            client.fetch_group_members("group-placeholder@chatroom")

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
