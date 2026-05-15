from __future__ import annotations

from .driver import DriverUnavailable


class TemplateDriver:
    def __init__(self) -> None:
        raise DriverUnavailable("Template driver is kept as a fallback path and is not implemented in this phase.")
