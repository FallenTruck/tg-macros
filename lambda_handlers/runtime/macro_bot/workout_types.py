"""Workout lifecycle values and public application errors."""

SESSION_STATUS_IN_PROGRESS = "in_progress"
SESSION_STATUS_COMPLETED = "completed"
SESSION_STATUS_CANCELLED = "cancelled"
EXECUTION_STATUS_PENDING = "pending"
EXECUTION_STATUS_IN_PROGRESS = "in_progress"
EXECUTION_STATUS_COMPLETED = "completed"
EXECUTION_STATUS_SKIPPED = "skipped"
SET_STATUS_COMPLETED = "completed"
SET_STATUS_SKIPPED = "skipped"
SET_TYPES = {"working", "warmup"}
SKIP_REASONS = {
    "intentionally_skipped",
    "recently_trained",
    "time_constraint",
    "equipment_unavailable",
    "fatigue",
    "discomfort",
    "other",
}


class WorkoutNotFound(LookupError):
    """A workout resource is not owned by or available to the caller."""


class WorkoutConflict(RuntimeError):
    """A conditional write lost a race or used a stale revision."""


class InvalidWorkoutInput(ValueError):
    """The authenticated caller supplied an invalid workout value."""
