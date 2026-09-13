"""Workout programme persistence contract and publication errors."""
from typing import Any, Optional, Protocol

from .data_errors import DataError


class ProgrammeSeedConflict(DataError):
    """Raised when a deterministic programme key contains different data."""

    pass


class ProgrammeReader(Protocol):
    def __call__(self, version_id: Optional[str] = None) -> Optional[dict[str, Any]]: ...


class ProgrammeRepository(Protocol):
    def get_programme(self, version_id: Optional[str] = None) -> Optional[dict[str, Any]]: ...
    def get_programme_day(self, day_code: str, version_id: Optional[str] = None) -> Optional[dict[str, Any]]: ...
    def seed_workout_programme(self, *, dry_run: bool = False) -> dict[str, int]: ...
    def publish_core_options_programme(self, *, dry_run: bool = False) -> dict[str, Any]: ...
