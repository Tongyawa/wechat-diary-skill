from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wechat_diary_core.config import load_config
from wechat_diary_core.weflow_automation.cdp_driver import TaskRow
from wechat_diary_core.weflow_automation.voice_transcribe import batch_transcribe_voices_for


class FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def click_by_name(self, name: str, retries: int = 3) -> None:
        self.calls.append(("click", name, None))

    def click_if_present(self, name: str, timeout: float = 2) -> bool:
        self.calls.append(("click_if_present", name, str(int(timeout))))
        return True

    def click_after_anchor(self, anchor: str, target: str, timeout: float = 30) -> None:
        self.calls.append(("click_after_anchor", anchor, target))

    def set_text(self, field_name: str, text: str) -> None:
        self.calls.append(("set_text", field_name, text))

    def wait_for(self, name: str, timeout: float = 60) -> None:
        self.calls.append(("wait_for", name, str(int(timeout))))

    def wait_for_absent(self, name: str, timeout: float = 60) -> None:
        self.calls.append(("wait_for_absent", name, str(int(timeout))))

    def wait_for_enabled(self, name: str, timeout: float = 60) -> None:
        self.calls.append(("wait_for_enabled", name, str(int(timeout))))

    def wait_for_text_sequence(self, first: str, second: str, timeout: float = 60) -> None:
        self.calls.append(("wait_for_text_sequence", f"{first}->{second}", str(int(timeout))))

    def ensure_selected(self, name: str, timeout: float = 60) -> None:
        self.calls.append(("ensure_selected", name, str(int(timeout))))

    def ensure_checked(self, name: str, timeout: float = 60) -> None:
        self.calls.append(("ensure_checked", name, str(int(timeout))))

    def ensure_action_available(self, action_name: str, trigger_name: str, timeout: float = 60) -> None:
        self.calls.append(("ensure_action_available", action_name, trigger_name))

    def close_any_modal(self, timeout: float = 5) -> int:
        self.calls.append(("close_any_modal", "", str(int(timeout))))
        return 0

    def close_current_modal(self, timeout: float = 5) -> bool:
        self.calls.append(("close_current_modal", "", str(int(timeout))))
        return False

    def snapshot_task_rows(self):
        self.calls.append(("snapshot_task_rows", "", None))
        return []

    def wait_for_new_task_completion(
        self,
        baseline,
        title_contains: str,
        status: str = "已完成",
        timeout: float = 1800,
        poll_interval: float = 1.0,
    ):
        self.calls.append(("wait_for_new_task_completion", title_contains, str(int(timeout))))
        return TaskRow(title=title_contains, status=status, signature="fake")

    def screenshot(self) -> bytes:
        return b""


def _test_config(tmp_dir: Path) -> object:
    config_path = tmp_dir / "config.toml"
    config_path.write_text(
        f"""
[paths]
raw = "{(tmp_dir / 'raw').as_posix()}"

[automation]
weflow_exe = "{(tmp_dir / 'WeFlow.exe').as_posix()}"
""".strip(),
        encoding="utf-8",
    )
    return load_config(config_path)


class VoiceTranscribeTests(unittest.TestCase):
    def test_empty_usernames_is_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _test_config(Path(tmp))
            driver = FakeDriver()

            result = batch_transcribe_voices_for([], config=cfg, driver=driver)

        self.assertEqual(result, [])
        self.assertEqual(driver.calls, [])

    def test_single_username_runs_thirteen_step_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _test_config(Path(tmp))
            driver = FakeDriver()

            runs = batch_transcribe_voices_for(["Contact"], config=cfg, driver=driver)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].username, "Contact")

        # Spot-check the key invariants of the sequence
        kinds = [(call[0], call[1]) for call in driver.calls]
        self.assertIn(("click", "聊天"), kinds)
        self.assertIn(("set_text", "搜索"), [(c[0], c[1]) for c in driver.calls])
        self.assertIn(("click_after_anchor", "联系人"), kinds)
        self.assertIn(("ensure_checked", "批量转文字"), kinds)
        self.assertIn(("snapshot_task_rows", ""), kinds)
        self.assertIn(("click", "开始转写"), kinds)
        self.assertIn(("wait_for_new_task_completion", "语音批量转写(Contact)"), kinds)
        self.assertIn(("click", "首页"), kinds)

    def test_two_usernames_run_back_to_back_with_distinct_baselines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _test_config(Path(tmp))
            driver = FakeDriver()

            runs = batch_transcribe_voices_for(["A", "B"], config=cfg, driver=driver)

        self.assertEqual([r.username for r in runs], ["A", "B"])
        wait_titles = [call[1] for call in driver.calls if call[0] == "wait_for_new_task_completion"]
        self.assertEqual(wait_titles, ["语音批量转写(A)", "语音批量转写(B)"])


if __name__ == "__main__":
    unittest.main()
