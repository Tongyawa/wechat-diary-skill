class PreprocessingError(Exception):
    """Base error for export preprocessing."""


class InvalidExportError(PreprocessingError):
    """Raised when a JSON file does not match the expected chat export shape."""
