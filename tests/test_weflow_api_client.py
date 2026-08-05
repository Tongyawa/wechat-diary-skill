from __future__ import annotations

import json
import unittest
from datetime import date
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
