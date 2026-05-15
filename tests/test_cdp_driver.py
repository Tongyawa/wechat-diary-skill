from __future__ import annotations

import base64
import unittest

from wechat_diary_core.weflow_automation.cdp_driver import CdpDriver, _click_script, select_page_target


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

    def test_click_wait_set_text_enabled_snapshot_and_screenshot_use_cdp(self) -> None:
        connection = FakeConnection(
            values=[
                {"ok": True},
                {"ok": True},
                {"ok": True},
                {"ok": True},
                {"ok": True},
                [{"tag": "BUTTON", "text": "开始导出", "enabled": True}],
            ]
        )
        driver = CdpDriver(connection)  # type: ignore[arg-type]

        driver.click_by_name("导出")
        self.assertTrue(driver.click_if_present("关闭任务中心", timeout=0.1))
        driver.set_text("查找", "abc")
        driver.wait_for("已完成", timeout=0.1)
        driver.wait_for_enabled("开始导出", timeout=0.1)
        self.assertEqual(driver.visible_elements(), [{"tag": "BUTTON", "text": "开始导出", "enabled": True}])
        self.assertEqual(driver.screenshot(), b"png")
        driver.close()

        methods = [method for method, _ in connection.calls]
        self.assertIn("Runtime.enable", methods)
        self.assertEqual(methods.count("Runtime.evaluate"), 6)
        self.assertIn("Page.captureScreenshot", methods)
        self.assertEqual(methods[-1], "close")

    def test_click_script_routes_readonly_inputs_to_sibling_button(self) -> None:
        script = _click_script("点击选择输出目录")

        self.assertIn('input[readonly]', script)
        self.assertIn("parentElement", script)
        self.assertIn("querySelector", script)

    def test_click_script_activates_target_once(self) -> None:
        script = _click_script("点击选择输出目录")

        self.assertIn('"click"', script)
        self.assertNotIn("element.click();", script)


if __name__ == "__main__":
    unittest.main()
