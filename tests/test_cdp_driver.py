from __future__ import annotations

import base64
import unittest

from wechat_diary_core.weflow_automation.cdp_driver import CdpDriver, select_page_target


class FakeConnection:
    def __init__(self, values: list[object] | None = None) -> None:
        self.values = values or []
        self.calls: list[tuple[str, dict[str, object]]] = []

    def send(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self.calls.append((method, params or {}))
        if method == "Runtime.evaluate":
            value = self.values.pop(0)
            return {"result": {"value": value}}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"png").decode("ascii")}
        return {}

    def close(self) -> None:
        self.calls.append(("close", {}))


class CdpDriverTests(unittest.TestCase):
    def test_select_prefers_home_over_notification(self) -> None:
        target = select_page_target(
            [
                {
                    "type": "page",
                    "id": "notification",
                    "title": "WeFlow",
                    "url": "file:///app/index.html#/notification-window",
                    "webSocketDebuggerUrl": "ws://127.0.0.1/notification",
                },
                {
                    "type": "page",
                    "id": "home",
                    "title": "WeFlow",
                    "url": "file:///app/index.html#/home",
                    "webSocketDebuggerUrl": "ws://127.0.0.1/home",
                },
            ]
        )

        self.assertIsNotNone(target)
        self.assertEqual(target.id, "home")

    def test_click_wait_set_text_and_screenshot_use_cdp(self) -> None:
        connection = FakeConnection(values=[{"ok": True}, {"ok": True}, {"ok": True}])
        driver = CdpDriver(connection)  # type: ignore[arg-type]

        driver.click_by_name("导出")
        driver.set_text("查找", "abc")
        driver.wait_for("已完成", timeout=0.1)
        self.assertEqual(driver.screenshot(), b"png")
        driver.close()

        methods = [method for method, _ in connection.calls]
        self.assertIn("Runtime.enable", methods)
        self.assertEqual(methods.count("Runtime.evaluate"), 3)
        self.assertIn("Page.captureScreenshot", methods)
        self.assertEqual(methods[-1], "close")


if __name__ == "__main__":
    unittest.main()
