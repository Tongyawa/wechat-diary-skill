from __future__ import annotations

import base64
import unittest

from wechat_diary_core.weflow_automation.cdp_driver import (
    CdpDriver,
    POST_CLICK_DELAY_SEC,
    POST_TEXT_DELAY_SEC,
    _click_script,
    _close_modal_script,
    select_page_target,
)


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
        click_result = {"ok": True}
        connection = FakeConnection(
            values=[
                click_result,
                click_result,
                {"ok": True},
                {"ok": True},
                {"ok": False},
                {"ok": True},
                {"ok": True},
                {"ok": True},
                {"ok": True, "checked": True},
                [{"tag": "BUTTON", "text": "开始导出", "enabled": True}],
            ]
        )
        driver = CdpDriver(connection)  # type: ignore[arg-type]

        driver.click_by_name("导出")
        self.assertTrue(driver.click_if_present("关闭任务中心", timeout=0.1))
        driver.set_text("查找", "abc")
        driver.wait_for("已完成", timeout=0.1)
        driver.wait_for_absent("导出格式", timeout=0.1)
        driver.wait_for_enabled("开始导出", timeout=0.1)
        driver.wait_for_text_sequence("联系人", "abc", timeout=0.1)
        driver.ensure_selected("abc", timeout=0.1)
        driver.ensure_checked("图片", timeout=0.1)
        self.assertEqual(driver.visible_elements(), [{"tag": "BUTTON", "text": "开始导出", "enabled": True}])
        self.assertEqual(driver.screenshot(), b"png")
        driver.close()

        methods = [method for method, _ in connection.calls]
        self.assertIn("Runtime.enable", methods)
        self.assertEqual(methods.count("Runtime.evaluate"), 10)
        self.assertIn("Page.captureScreenshot", methods)
        self.assertEqual(methods[-1], "close")

    def test_click_script_routes_readonly_inputs_to_sibling_button(self) -> None:
        script = _click_script("点击选择输出目录")

        self.assertIn('input[readonly]', script)
        self.assertIn("parentElement", script)
        self.assertIn("querySelector", script)

    def test_click_script_prefers_active_modal_scope(self) -> None:
        script = _click_script("图片")

        self.assertIn("activeSearchRoots", script)
        self.assertIn(".modal-overlay,.export-dialog,[role='dialog'],.modal", script)
        self.assertIn("label,[contenteditable='true']", script)

    def test_click_script_activates_target_once(self) -> None:
        script = _click_script("点击选择输出目录")

        self.assertIn("clickElement(clickable)", script)
        self.assertNotIn("clickable.click();", script)

    def test_click_script_uses_native_click_for_checkbox_labels(self) -> None:
        script = _click_script("图片")

        self.assertIn("label,input[type='checkbox'],input[type='radio']", script)
        self.assertIn("button,a,input,textarea,label", script)
        self.assertIn("element.click();", script)

    def test_checkbox_state_script_finds_label_checkbox(self) -> None:
        from wechat_diary_core.weflow_automation.cdp_driver import _checkbox_state_script

        script = _checkbox_state_script("图片")

        self.assertIn("input[type='checkbox'],input[type='radio']", script)
        self.assertIn("activeSearchRoots", script)

    def test_close_modal_script_only_searches_active_modals(self) -> None:
        script = _close_modal_script()

        self.assertIn("root !== document", script)
        self.assertIn('"关闭"', script)
        self.assertIn("no active modal close control", script)

    def test_click_delay_is_nonzero_for_gui_stability(self) -> None:
        self.assertGreaterEqual(POST_CLICK_DELAY_SEC, 0.3)
        self.assertGreaterEqual(POST_TEXT_DELAY_SEC, 0.5)

    def test_snapshot_task_rows_normalizes_payload(self) -> None:
        connection = FakeConnection(
            values=[
                [
                    {"title": "自动化导出 2026-05-15", "status": "已完成", "signature": "自动化导出 2026-05-15 已完成"},
                    {"title": "", "status": "", "signature": ""},
                    "not a dict",
                ]
            ]
        )
        driver = CdpDriver(connection)  # type: ignore[arg-type]

        rows = driver.snapshot_task_rows()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, "自动化导出 2026-05-15")
        self.assertEqual(rows[0].status, "已完成")

    def test_wait_for_new_task_completion_skips_baseline_and_returns_new_done(self) -> None:
        connection = FakeConnection(
            values=[
                # poll 1: baseline-only rows + a fresh 进行中 row
                [
                    {"title": "自动化导出 旧", "status": "已完成", "signature": "自动化导出 旧 已完成"},
                    {"title": "自动化导出 新", "status": "进行中", "signature": "自动化导出 新 进行中"},
                ],
                # poll 2: 新 row flips to 已完成
                [
                    {"title": "自动化导出 旧", "status": "已完成", "signature": "自动化导出 旧 已完成"},
                    {"title": "自动化导出 新", "status": "已完成", "signature": "自动化导出 新 已完成"},
                ],
            ]
        )
        driver = CdpDriver(connection)  # type: ignore[arg-type]
        baseline = {"自动化导出 旧 已完成"}

        result = driver.wait_for_new_task_completion(
            baseline=baseline,
            title_contains="自动化导出",
            status="已完成",
            timeout=5,
            poll_interval=0.01,
        )

        self.assertEqual(result.title, "自动化导出 新")
        self.assertEqual(result.status, "已完成")

    def test_wait_for_new_task_completion_raises_on_timeout(self) -> None:
        from wechat_diary_core.weflow_automation.driver import ElementNotFound

        connection = FakeConnection(values=[[] for _ in range(200)])
        driver = CdpDriver(connection)  # type: ignore[arg-type]

        with self.assertRaises(ElementNotFound):
            driver.wait_for_new_task_completion(
                baseline=set(),
                title_contains="自动化导出",
                status="已完成",
                timeout=0.05,
                poll_interval=0.01,
            )

    def test_wait_for_new_task_completion_raises_on_failed_new_row(self) -> None:
        from wechat_diary_core.weflow_automation.driver import TaskFailed

        connection = FakeConnection(
            values=[
                [
                    {"title": "语音批量转写（联系人）", "status": "失败", "signature": "语音批量转写（联系人） 失败"},
                ]
            ]
        )
        driver = CdpDriver(connection)  # type: ignore[arg-type]

        with self.assertRaises(TaskFailed):
            driver.wait_for_new_task_completion(
                baseline=set(),
                title_contains="语音批量转写",
                status="已完成",
                timeout=5,
                poll_interval=0.01,
            )


if __name__ == "__main__":
    unittest.main()
