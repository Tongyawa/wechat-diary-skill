from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from wechat_diary_core.backends import REGISTRY, create_backend
from wechat_diary_core.backends.manual import ManualBackend
from wechat_diary_core.backends.weflow.backend import WeflowBackend
from wechat_diary_core.backends.weflow_api.backend import WeflowApiBackend
from wechat_diary_core.config import load_config


def _config(root: Path, *, restart: bool = True):
    path = root / "config.toml"
    path.write_text(
        f"""
[paths]
raw = "{(root / 'raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
archived = "{(root / 'archived').as_posix()}"

[automation]
weflow_exe = "{(root / 'WeFlow.exe').as_posix()}"

[daily_export]
restart_weflow = {str(restart).lower()}
self_moments_usernames = []
""".strip(),
        encoding="utf-8",
    )
    return load_config(path)


class WeflowBackendTests(unittest.TestCase):
    def test_registry_contains_phase_b_api_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))

        self.assertEqual(set(REGISTRY), {"weflow", "weflow_api", "manual"})
        self.assertIsInstance(create_backend("manual", cfg), ManualBackend)
        self.assertIsInstance(create_backend("weflow_api", cfg), WeflowApiBackend)
        self.assertEqual(WeflowApiBackend.capabilities, frozenset({"moments"}))

    def test_wraps_legacy_operations_in_port_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            calls: list[tuple] = []
            backend = WeflowBackend(
                cfg,
                stop_processes=lambda timeout: calls.append(("stop", int(timeout))),
                ensure_running=lambda config: calls.append(("start",))
                or SimpleNamespace(cdp_endpoint="http://127.0.0.1:9222"),
                wait_ready=lambda endpoint: calls.append(("ready", endpoint)),
                export_all=lambda date, config, cleanup: calls.append(("chats", date.isoformat(), cleanup)),
                export_moments_for=lambda usernames, date, config: calls.append(
                    ("moments", tuple(usernames), date.isoformat())
                ),
                wait_tasks_idle=lambda config, title_contains: calls.append(("idle", title_contains)),
                transcribe_for=lambda usernames, config: calls.append(("voice", tuple(usernames))),
            )

            backend.prepare()
            backend.transcribe_voices(["Voice"])
            backend.export_chats(date(2026, 5, 16))
            backend.export_moments(["Target"], date(2026, 5, 16))
            backend.export_moments(["Self"], date(2026, 5, 16))
            backend.shutdown()

        self.assertEqual(
            calls,
            [
                ("stop", 90),
                ("start",),
                ("ready", "http://127.0.0.1:9222"),
                ("voice", ("Voice",)),
                ("chats", "2026-05-16", "skip"),
                ("idle", "自动化导出"),
                ("moments", ("Target",), "2026-05-16"),
                ("moments", ("Self",), "2026-05-16"),
            ],
        )

    def test_prepare_fails_before_launch_when_stop_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            starts: list[str] = []
            backend = WeflowBackend(
                cfg,
                stop_processes=lambda timeout: False,
                ensure_running=lambda config: starts.append("start"),
            )

            with self.assertRaisesRegex(RuntimeError, "Timed out waiting"):
                backend.prepare()

        self.assertEqual(starts, [])


if __name__ == "__main__":
    unittest.main()
