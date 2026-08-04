"""Compatibility shim for the legacy WeFlow automation import path."""

from __future__ import annotations

import sys
import warnings

from wechat_diary_core.backends import weflow as _weflow
from wechat_diary_core.backends.weflow import *  # noqa: F401,F403


warnings.warn(
    "wechat_diary_core.weflow_automation is deprecated; "
    "use wechat_diary_core.backends.weflow instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = _weflow.__all__

for _module_name in (
    "cdp_driver",
    "driver",
    "exporter",
    "launcher",
    "native_dialog",
    "ocr",
    "template_driver",
    "uia_driver",
    "voice_transcribe",
):
    _module = __import__(
        f"wechat_diary_core.backends.weflow.{_module_name}",
        fromlist=[_module_name],
    )
    globals()[_module_name] = _module
    sys.modules[f"{__name__}.{_module_name}"] = _module

del _module, _module_name, _weflow
