"""Shared durable-data error base, independent of repository implementations."""


class DataError(RuntimeError):
    """Base class for durable application data errors."""
