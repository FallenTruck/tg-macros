"""Workout lifecycle, validation, orchestration and response assembly."""
from __future__ import annotations
import uuid
import math
import re
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional, Callable
from .workout_programme import day_response
from .workout_repository import WorkoutRepository, WorkoutIdentity
from .programme_repository import ProgrammeReader
from .workout_types import (
    EXECUTION_STATUS_PENDING,
    EXECUTION_STATUS_SKIPPED,
    InvalidWorkoutInput,
    SESSION_STATUS_IN_PROGRESS,
    SET_STATUS_COMPLETED,
    SET_STATUS_SKIPPED,
    SET_TYPES,
    SKIP_REASONS,
    WorkoutConflict,
    WorkoutNotFound,
)

class WorkoutService:
    def __init__(self, repository: WorkoutRepository, programme_reader: ProgrammeReader, *, now_fn: Callable = lambda: datetime.now(timezone.utc), session_id_factory: Callable = lambda: uuid.uuid4().hex):
        self.repository = repository
        self.programme_reader = programme_reader
        self.now_fn = now_fn
        self.session_id_factory = session_id_factory

    def _now(self) -> str:
        value = self.now_fn()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _payload(self, identity, session):
        executions = sorted(self.repository.get_executions(identity, session), key=lambda item: int(item.get("prescription_sequence", 0)))
        return {"session": dict(session), "executions": [
            {**execution, "sets": self.repository.get_sets(identity, session, execution)} for execution in executions
        ]}

    def list_workout_history(self, identity, *, limit=20, cursor=None):
        if isinstance(limit, bool) or not re.fullmatch(r"[0-9]{1,2}", str(limit)) or not 1 <= int(limit) <= 50:
            raise InvalidWorkoutInput("limit must be an integer between 1 and 50")
        page = self.repository.list_history(identity, limit=limit, cursor=cursor)
        return {**page, "sessions": [self._summary(item) for item in page["sessions"]]}

    @staticmethod
    def _summary(item: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(item)
        try:
            start = datetime.fromisoformat(result["started_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(result["completed_at"].replace("Z", "+00:00"))
            seconds = (end - start).total_seconds()
            if seconds >= 0:
                result["duration_minutes"] = int(seconds // 60)
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        return result

    @staticmethod
    def _as_int(value: Any, field: str, *, minimum: int = 1) -> int:
        if isinstance(value, bool):
            raise InvalidWorkoutInput(f"{field} must be an integer")
        try:
            result = int(value)
        except (TypeError, ValueError) as err:
            raise InvalidWorkoutInput(f"{field} must be an integer") from err
        if result < minimum:
            raise InvalidWorkoutInput(f"{field} must be at least {minimum}")
        return result

    @staticmethod
    def _as_float(value: Any, field: str, *, minimum: float = 0.0, maximum: Optional[float] = None) -> float:
        if isinstance(value, bool):
            raise InvalidWorkoutInput(f"{field} must be numeric")
        try:
            result = float(value)
        except (TypeError, ValueError) as err:
            raise InvalidWorkoutInput(f"{field} must be numeric") from err
        if result < minimum or (maximum is not None and result > maximum):
            raise InvalidWorkoutInput(f"{field} is outside the allowed range")
        return result

    def _require_session(self, identity: WorkoutIdentity, session_id: str) -> dict[str, Any]:
        session = self.repository.get_session(identity, session_id)
        if not session:
            raise WorkoutNotFound("Workout session was not found")
        return session

    def _require_in_progress(self, identity: WorkoutIdentity, session_id: str) -> dict[str, Any]:
        session = self._require_session(identity, session_id)
        if session.get("status") != SESSION_STATUS_IN_PROGRESS:
            raise WorkoutConflict("Workout session is not in progress")
        return session

    def _require_execution(self, identity: WorkoutIdentity, session_id: str, execution_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        session = self._require_in_progress(identity, session_id)
        execution = self.repository.get_execution(identity, session, execution_id)
        if not execution:
            raise WorkoutNotFound("Workout execution was not found")
        return session, execution

    @staticmethod
    def _resolved_working_sets(sets: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Return working sets that resolve a prescribed working-set slot.

        Older persisted sets may not have ``set_type`` or ``status``. They were
        completed sets by definition, so retain their historical meaning while
        excluding warm-ups from completion requirements.
        """

        return [
            item
            for item in sets
            if str(item.get("set_type", "working")).strip().lower() == "working"
            and str(item.get("status", SET_STATUS_COMPLETED)).strip().lower()
            in {SET_STATUS_COMPLETED, SET_STATUS_SKIPPED}
        ]

    @staticmethod
    def _expected_revision(payload: Mapping[str, Any], current: int) -> int:
        if "expected_revision" not in payload:
            raise WorkoutConflict("expected_revision is required")
        expected = WorkoutService._as_int(payload.get("expected_revision"), "expected_revision", minimum=0)
        if expected != current:
            raise WorkoutConflict("Workout state is stale; reload and retry")
        return expected

    def _validate_set_payload(self, execution: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        status = str(payload.get("status", SET_STATUS_COMPLETED)).strip().lower()
        if status not in {SET_STATUS_COMPLETED, SET_STATUS_SKIPPED}:
            raise InvalidWorkoutInput("set status is invalid")
        set_type = str(payload.get("set_type", "working")).strip().lower()
        if set_type not in SET_TYPES:
            raise InvalidWorkoutInput("set_type is invalid")
        result: dict[str, Any] = {
            "set_type": set_type,
            "status": status,
            "load_value": None,
            "load_unit": str(payload.get("load_unit", "kg") or "kg"),
            "load_scope": str(payload.get("load_scope", "equipment") or "equipment"),
            "reps": None,
            "side_reps": None,
            "duration_seconds": None,
            "rir": None,
            "skip_reason": "",
            "notes": str(payload.get("notes", "") or "")[:500],
        }
        if status == SET_STATUS_SKIPPED:
            reason = str(payload.get("skip_reason", "")).strip() or "intentionally_skipped"
            if reason not in SKIP_REASONS:
                raise InvalidWorkoutInput("skip_reason is invalid")
            result["skip_reason"] = reason
            return result
        execution_type = str(execution.get("execution_type", "loaded_reps"))
        if execution_type == "loaded_reps":
            if payload.get("load_value") is None:
                raise InvalidWorkoutInput("load_value is required for a loaded set")
            result["load_value"] = self._as_float(payload.get("load_value"), "load_value", minimum=0)
            result["reps"] = self._as_int(payload.get("reps"), "reps")
        elif execution_type == "bodyweight_reps":
            if payload.get("load_value") is not None:
                raise InvalidWorkoutInput("bodyweight sets must not include load_value")
            result["reps"] = self._as_int(payload.get("reps"), "reps")
        elif execution_type == "optional_load_reps":
            load = payload.get("load_value")
            if load is not None:
                load = self._as_float(load, "load_value", minimum=0)
                if not math.isfinite(load):
                    raise InvalidWorkoutInput("load_value must be finite")
            result["load_value"] = load if load else None
            result["load_scope"] = "equipment" if load else "bodyweight"
            result["reps"] = self._as_int(payload.get("reps"), "reps")
        elif execution_type == "side_aware_reps":
            sides = payload.get("side_reps")
            if not isinstance(sides, Mapping):
                raise InvalidWorkoutInput("side_reps with left and right is required")
            result["side_reps"] = {"left": self._as_int(sides.get("left"), "left reps"), "right": self._as_int(sides.get("right"), "right reps")}
            if payload.get("load_value") is None:
                raise InvalidWorkoutInput("load_value is required for a loaded side-aware set")
            result["load_value"] = self._as_float(payload.get("load_value"), "load_value", minimum=0)
        elif execution_type == "timed":
            result["duration_seconds"] = self._as_int(payload.get("duration_seconds"), "duration_seconds")
        else:
            raise InvalidWorkoutInput("unsupported execution type")
        if payload.get("rir") is not None:
            result["rir"] = self._as_float(payload.get("rir"), "rir", minimum=0, maximum=10)
        if result["load_value"] is not None and execution.get("loading_convention") == "per_dumbbell_kg":
            result["load_scope"] = "per_dumbbell"
        return result

    def skip_set(self, identity: WorkoutIdentity, session_id: str, execution_id: str, ordinal: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        payload_with_status = dict(payload)
        payload_with_status["status"] = SET_STATUS_SKIPPED
        return self.put_set(identity, session_id, execution_id, ordinal, payload_with_status)

    def start_session(self, identity: WorkoutIdentity, day_code: str, *, actual_local_date: date | str) -> dict[str, Any]:
        has_pointer, active_session = self.repository.get_active_session(identity)
        if has_pointer:
            if active_session and active_session.get("status") == SESSION_STATUS_IN_PROGRESS:
                return self._payload(identity, active_session)
            raise WorkoutConflict("An active workout pointer is inconsistent")

        programme = self.programme_reader()
        if programme is None:
            raise WorkoutNotFound("Shared workout programme is unavailable")
        requested_day = str(day_code or "").strip().upper()
        day_result = day_response(programme, requested_day)
        if day_result is None:
            raise WorkoutNotFound("Workout programme day was not found")
        day = day_result["day"]
        session_id = str(self.session_id_factory())
        local_date_text = actual_local_date.isoformat() if isinstance(actual_local_date, date) else str(actual_local_date)
        now = self._now()
        session_item = {
            "session_id": session_id,
            "user_id": identity.user_id,
            "programme_id": programme["programme"].get("programme_id"),
            "programme_version_id": programme["version"].get("version_id"),
            "programme_day_id": requested_day,
            "planned_weekday": day.get("planned_weekday"),
            "workout_name": day.get("display_name", requested_day),
            "actual_local_date": local_date_text,
            "started_at": now,
            "completed_at": None,
            "status": SESSION_STATUS_IN_PROGRESS,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        }
        exercise_map = {str(item["exercise_id"]): item for item in programme.get("exercises", [])}
        execution_items = []
        for prescription in day.get("prescriptions", []):
            sequence = self._as_int(prescription.get("sequence"), "prescription sequence")
            default_exercise_id = prescription.get("default_exercise_id")
            default_exercise = exercise_map.get(str(default_exercise_id), {})
            option_targets = prescription.get("option_targets", {}) or {}
            selected_target = option_targets.get(str(default_exercise_id), {})
            execution_items.append(
                {
                    "execution_id": f"{session_id}:{sequence:03d}",
                    "session_id": session_id,
                    "programme_id": programme["programme"].get("programme_id"),
                    "programme_version_id": programme["version"].get("version_id"),
                    "programme_day_id": requested_day,
                    "prescription_id": prescription.get("prescription_id"),
                    "prescription_sequence": sequence,
                    "prescribed_default_exercise_id": default_exercise_id,
                    "performed_exercise_id": default_exercise_id,
                    "allowed_exercise_ids": list(prescription.get("allowed_exercise_ids", [])),
                    "substitution_reason": "",
                    "skip_reason": "",
                    "prescribed_set_count_min": selected_target.get("set_min", prescription.get("set_min")),
                    "prescribed_set_count_max": selected_target.get("set_max", prescription.get("set_max")),
                    "prescribed_min_reps": selected_target.get("rep_min", prescription.get("rep_min")),
                    "prescribed_max_reps": selected_target.get("rep_max", prescription.get("rep_max")),
                    "prescribed_duration_seconds": selected_target.get("duration_seconds"),
                    "execution_type": selected_target.get("execution_type", default_exercise.get("execution_type", "loaded_reps")),
                    "unilateral_mode": default_exercise.get("unilateral_mode", "bilateral"),
                    "loading_convention": default_exercise.get("loading_convention", "none"),
                    "optional": bool(prescription.get("optional", False)),
                    "option_targets": option_targets,
                    "notes": prescription.get("notes", ""),
                    "status": EXECUTION_STATUS_PENDING,
                    "revision": 1,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        try:
            self.repository.create_session(identity, session_item, execution_items)
        except WorkoutConflict:
            _, existing = self.repository.get_active_session(identity)
            if existing and existing.get("status") == SESSION_STATUS_IN_PROGRESS:
                return self._payload(identity, existing)
            raise
        return self._payload(identity, session_item)

    def get_active_session(self, identity):
        _, session = self.repository.get_active_session(identity)
        if not session or session.get("status") != SESSION_STATUS_IN_PROGRESS:
            return None
        return self._payload(identity, session)

    def get_session(self, identity: WorkoutIdentity, session_id: str) -> dict[str, Any]:
        session = self._require_session(identity, session_id)
        result = self._payload(identity, session)
        if session.get("status") != SESSION_STATUS_IN_PROGRESS:
            programme = self.programme_reader(version_id=session.get("programme_version_id")) or {}
            names = {item["exercise_id"]: item.get("canonical_name", item["exercise_id"]) for item in programme.get("exercises", [])}
            for execution in result["executions"]:
                execution["exercise_name"] = names.get(execution["performed_exercise_id"], execution["performed_exercise_id"])
            result["session"].update(self._summary({key: session.get(key) for key in ("session_id", "programme_day_id", "workout_name", "programme_version_id", "actual_local_date", "started_at", "completed_at", "status")}))
        return result

    def select_exercise(self, identity: WorkoutIdentity, session_id: str, execution_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        session, execution = self._require_execution(identity, session_id, execution_id)
        performed = str(payload.get("performed_exercise_id", "")).strip()
        allowed = {str(item) for item in execution.get("allowed_exercise_ids", [])}
        if performed not in allowed:
            raise InvalidWorkoutInput("performed_exercise_id is not allowed for this prescription")
        if self.repository.get_sets(identity, session, execution):
            raise WorkoutConflict("Exercise choice cannot change after sets are logged")
        expected = self._expected_revision(payload, int(execution.get("revision", 0)))
        if performed == str(execution.get("performed_exercise_id", "")):
            return self._payload(identity, session)
        options = execution.get("option_targets", {}) or {}
        target = options.get(performed, {})
        programme = self.programme_reader(version_id=execution.get("programme_version_id"))
        exercise_map = {str(item["exercise_id"]): item for item in (programme or {}).get("exercises", [])}
        exercise = exercise_map.get(performed, {})
        new_revision = expected + 1
        fields = {
            "performed_exercise_id": performed,
            "substitution_reason": str(payload.get("substitution_reason", "") or ""),
            "execution_type": target.get("execution_type", exercise.get("execution_type", execution.get("execution_type"))),
            "unilateral_mode": exercise.get("unilateral_mode", execution.get("unilateral_mode", "bilateral")),
            "loading_convention": exercise.get("loading_convention", execution.get("loading_convention", "none")),
            "prescribed_min_reps": target.get("rep_min"),
            "prescribed_max_reps": target.get("rep_max"),
            "prescribed_set_count_min": target.get("set_min", execution.get("prescribed_set_count_min")),
            "prescribed_set_count_max": target.get("set_max", execution.get("prescribed_set_count_max")),
            "prescribed_duration_seconds": target.get("duration_seconds"),
            "skip_reason": "", "revision": new_revision, "updated_at": self._now(),
        }
        self.repository.update_execution(identity, session, execution, fields, expected, selectable=True)
        return self._payload(identity, session)

    def skip_execution(self, identity: WorkoutIdentity, session_id: str, execution_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        session, execution = self._require_execution(identity, session_id, execution_id)
        reason = str(payload.get("skip_reason", "")).strip() or "intentionally_skipped"
        if reason not in SKIP_REASONS:
            raise InvalidWorkoutInput("skip_reason is invalid")
        if execution.get("status") == EXECUTION_STATUS_SKIPPED and execution.get("skip_reason") == reason and "expected_revision" not in payload:
            return self._payload(identity, session)
        expected = self._expected_revision(payload, int(execution.get("revision", 0)))
        self._update_execution_status(identity, session, execution, EXECUTION_STATUS_SKIPPED, reason, expected)
        return self._payload(identity, session)

    def reset_execution(self, identity: WorkoutIdentity, session_id: str, execution_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        session, execution = self._require_execution(identity, session_id, execution_id)
        expected = self._expected_revision(payload, int(execution.get("revision", 0)))
        self._update_execution_status(identity, session, execution, EXECUTION_STATUS_PENDING, "", expected)
        return self._payload(identity, session)

    def _update_execution_status(self, identity, session, execution, status, skip_reason, expected):
        self.repository.update_execution(identity, session, execution, {
            "status": status, "skip_reason": skip_reason, "revision": expected + 1, "updated_at": self._now(),
        }, expected)

    def put_set(self, identity: WorkoutIdentity, session_id: str, execution_id: str, ordinal: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        session, execution = self._require_execution(identity, session_id, execution_id)
        if execution.get("status") == EXECUTION_STATUS_SKIPPED:
            raise WorkoutConflict("Skipped exercise must be reset before logging sets")
        set_ordinal = self._as_int(ordinal, "set ordinal")
        fields = self._validate_set_payload(execution, payload)
        existing = self.repository.get_set(identity, session, execution, set_ordinal)
        current_set_revision = int(existing.get("revision", 0)) if existing else 0
        if existing:
            expected_set = self._expected_revision(payload, current_set_revision)
        else:
            expected_set = int(payload.get("expected_revision", 0) or 0)
            if expected_set != 0:
                raise WorkoutConflict("A new set must start at revision 0")
        execution_expected = self._expected_revision({"expected_revision": payload.get("execution_expected_revision", execution.get("revision", 0))}, int(execution.get("revision", 0)))
        now = self._now()
        item = {
            "set_id": f"{execution_id}:{set_ordinal:03d}",
            "session_id": session_id,
            "execution_id": execution_id,
            "set_ordinal": set_ordinal,
            **fields,
            "revision": current_set_revision + 1,
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
        }
        self.repository.save_set(identity, session, execution, item, expected_set, execution_expected, existing is not None)
        return self._payload(identity, session)

    def complete_session(self, identity: WorkoutIdentity, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        session = self._require_in_progress(identity, session_id)
        expected = self._expected_revision(payload, int(session.get("revision", 0)))
        executions = self.repository.get_executions(identity, session)
        blockers = []
        for execution in sorted(
            executions,
            key=lambda item: int(item.get("prescription_sequence", 0)),
        ):
            if execution.get("status") == EXECUTION_STATUS_SKIPPED:
                continue
            sets = self.repository.get_sets(identity, session, execution)
            minimum_sets = max(1, int(execution.get("prescribed_set_count_min") or 1))
            if len(self._resolved_working_sets(sets)) < minimum_sets:
                blockers.append(str(execution.get("prescription_sequence", "exercise")))
        if blockers:
            exercises = ", ".join(blockers)
            raise WorkoutConflict(f"Log or skip every exercise before submitting (incomplete: {exercises})")

        now = self._now()
        self.repository.complete_session(identity, session, executions, expected, now)
        updated = self.repository.get_session(identity, session_id)
        return self._payload(identity, updated or session)

    def cancel_session(self, identity: WorkoutIdentity, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        session = self._require_in_progress(identity, session_id)
        expected = self._expected_revision(payload, int(session.get("revision", 0)))
        now = self._now()
        self.repository.cancel_session(identity, session, expected, now)
        updated = self.repository.get_session(identity, session_id)
        return self._payload(identity, updated or session)
