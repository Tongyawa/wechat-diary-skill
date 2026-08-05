"""Shared canonical session-directory naming rules."""

from __future__ import annotations

import re


_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def sanitize_session_name(value: object, *, fallback: str = "未命名会话") -> str:
    """Return a stable Windows-safe display-name component.

    The type prefix and date suffix are deliberately added by callers; this
    helper owns only the shared display-name normalization.
    """

    cleaned = _INVALID_FILENAME_CHARS.sub("_", str(value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).rstrip(" .")
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:120].rstrip(" .") or fallback


__all__ = ["sanitize_session_name"]
