from __future__ import annotations

import importlib
import unittest
import warnings


class WeflowAutomationCompatibilityTests(unittest.TestCase):
    def test_legacy_package_warns_and_forwards_submodules(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            legacy = importlib.import_module("wechat_diary_core.weflow_automation")
            importlib.reload(legacy)

        old_exporter = importlib.import_module("wechat_diary_core.weflow_automation.exporter")
        new_exporter = importlib.import_module("wechat_diary_core.backends.weflow.exporter")

        self.assertIs(old_exporter, new_exporter)
        self.assertTrue(
            any(
                issubclass(item.category, DeprecationWarning)
                and "wechat_diary_core.backends.weflow" in str(item.message)
                for item in caught
            )
        )


if __name__ == "__main__":
    unittest.main()
