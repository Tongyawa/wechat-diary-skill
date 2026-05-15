from __future__ import annotations

from .driver import DriverUnavailable


class UiaDriver:
    def __init__(self) -> None:
        raise DriverUnavailable("UIA driver is kept as a fallback path and is not implemented in this phase.")
