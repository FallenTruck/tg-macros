"""Durable, user-owned workout execution and set logging.

The shared programme remains immutable and is read through the existing
nutrition repository.  This module owns only per-user mutable execution
state, using deterministic DynamoDB keys and conditional writes for the
single-active-session and optimistic-concurrency rules.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
import math
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional

from boto3.dynamodb.conditions import Key

from .workout_programme import day_response

from .serverless_data import (
    ServerlessIdentity,
    _from_storage,
    _is_conditional_failure,
    _to_storage,
    utc_iso,
)

SESSION_ACTIVE_SK = "WORKOUT#ACTIVE"
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


class WorkoutExecutionRepository:
    def __init__(self, nutrition_repository: Any, *, session_id_factory: Any = lambda: uuid.uuid4().hex):
        self.repository = nutrition_repository
        self.session_id_factory = session_id_factory

    @staticmethod
    def _user_pk(identity: ServerlessIdentity) -> str:
        return identity.pk

    @staticmethod
    def _session_sk(actual_local_date: str, session_id: str) -> str:
        return f"WORKOUT#{actual_local_date}#{session_id}"

    @staticmethod
    def _execution_sk(session_sk: str, sequence: int) -> str:
        return f"{session_sk}#EXEC#{sequence:03d}"

    @staticmethod
    def _set_sk(execution_sk: str, ordinal: int) -> str:
        return f"{execution_sk}#SET#{ordinal:03d}"

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

    def _active_item(self, identity: ServerlessIdentity) -> Optional[dict[str, Any]]:
        return self.repository._get({"PK": self._user_pk(identity), "SK": SESSION_ACTIVE_SK})

    def _session_item(self, identity: ServerlessIdentity, session_id: str) -> Optional[dict[str, Any]]:
        active = self._active_item(identity)
        if active and str(active.get("session_id")) == str(session_id):
            item = self.repository._get({"PK": str(active["session_pk"]), "SK": str(active["session_sk"])})
            if item:
                return item
        locator = self.repository._get({"PK": identity.pk, "SK": f"WORKOUT_SESSION#{session_id}"})
        if not locator:
            return None
        return self.repository._get({"PK": identity.pk, "SK": locator["session_sk"]})

    @staticmethod
    def locator_item(session: Mapping[str, Any]) -> dict[str, Any]:
        return {"PK": session["PK"], "SK": f"WORKOUT_SESSION#{session['session_id']}",
                "entity_type": "workout_session_locator", "session_sk": session["SK"]}

    @staticmethod
    def history_item(session: Mapping[str, Any]) -> dict[str, Any]:
        if session.get("started_at"):
            started = datetime.fromisoformat(session["started_at"].replace("Z", "+00:00"))
        else:
            # Retrospective entries can intentionally omit actual times. Use a
            # date-only ordering anchor, never expose it as a factual start time.
            local_date = date.fromisoformat(session["actual_local_date"])
            started = datetime(local_date.year, local_date.month, local_date.day, tzinfo=timezone.utc)
        if started.tzinfo is None:
            raise ValueError("Session start must include a timezone")
        order = started.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        fields = ("session_id", "programme_day_id", "workout_name", "programme_version_id",
                  "actual_local_date", "started_at", "completed_at", "status")
        return {"PK": session["PK"], "SK": f"WORKOUT_HISTORY#{order}#{session['session_id']}",
                "entity_type": "workout_history", **{key: session.get(key) for key in fields}}

    def _history_put(self, session: Mapping[str, Any], status: str, now: str) -> dict[str, Any]:
        return {"operation": "Put", "TableName": self.repository.table_name,
                "Item": self.history_item({**session, "status": status, "completed_at": now}),
                "ConditionExpression": "attribute_not_exists(PK)"}

    @staticmethod
    def _summary(item: Mapping[str, Any]) -> dict[str, Any]:
        result = {k: v for k, v in _from_storage(item).items() if k not in {"PK", "SK", "entity_type"}}
        try:
            start = datetime.fromisoformat(result["started_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(result["completed_at"].replace("Z", "+00:00"))
            seconds = (end - start).total_seconds()
            if seconds >= 0:
                result["duration_minutes"] = int(seconds // 60)
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        return result

    def list_workout_history(self, identity: ServerlessIdentity, *, limit: Any = 20,
                             cursor: Optional[str] = None) -> dict[str, Any]:
        if isinstance(limit, bool) or not re.fullmatch(r"[0-9]{1,2}", str(limit)) or not 1 <= int(limit) <= 50:
            raise InvalidWorkoutInput("limit must be an integer between 1 and 50")
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(identity.pk) & Key("SK").begins_with("WORKOUT_HISTORY#"),
            "ScanIndexForward": False, "ConsistentRead": True, "Limit": int(limit),
        }
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError()
                decoded = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if (set(decoded) != {"v", "pk", "sk"} or decoded["v"] != 1 or decoded["pk"] != identity.pk
                        or not isinstance(decoded["sk"], str)
                        or not re.fullmatch(r"WORKOUT_HISTORY#[0-9T:.Z-]+#[^#]+", decoded["sk"])):
                    raise ValueError()
                datetime.strptime(decoded["sk"].split("#")[1], "%Y-%m-%dT%H:%M:%S.%fZ")
                if self._encode_cursor(decoded["pk"], decoded["sk"]) != cursor:
                    raise ValueError()
                kwargs["ExclusiveStartKey"] = {"PK": identity.pk, "SK": decoded["sk"]}
            except (ValueError, TypeError, KeyError, binascii.Error):
                raise InvalidWorkoutInput("Invalid workout history cursor") from None
        page = self.repository.table.query(**kwargs)
        last = page.get("LastEvaluatedKey")
        return {"sessions": [self._summary(item) for item in page.get("Items", [])],
                "next_cursor": self._encode_cursor(last["PK"], last["SK"]) if last else None}

    @staticmethod
    def _encode_cursor(pk: str, sk: str) -> str:
        return base64.urlsafe_b64encode(json.dumps({"v": 1, "pk": pk, "sk": sk},
                                                  sort_keys=True, separators=(",", ":")).encode()).decode()

    def _execution_item(self, identity: ServerlessIdentity, session_item: Mapping[str, Any], execution_id: str) -> Optional[dict[str, Any]]:
        prefix = f"{session_item['SK']}#EXEC#"
        records = self.repository._query(
            Key("PK").eq(self._user_pk(identity)) & Key("SK").begins_with(prefix),
            ConsistentRead=True,
        )
        for item in records:
            if item.get("entity_type") == "workout_execution" and str(item.get("execution_id")) == str(execution_id):
                return item
        return None

    def _set_items(self, identity: ServerlessIdentity, execution_item: Mapping[str, Any]) -> list[dict[str, Any]]:
        prefix = f"{execution_item['SK']}#SET#"
        records = self.repository._query(
            Key("PK").eq(self._user_pk(identity)) & Key("SK").begins_with(prefix),
            ScanIndexForward=True,
            ConsistentRead=True,
        )
        return sorted(
            (item for item in records if item.get("entity_type") == "workout_set"),
            key=lambda item: int(item.get("set_ordinal", 0)),
        )

    def _payload(self, identity: ServerlessIdentity, session_item: Mapping[str, Any]) -> dict[str, Any]:
        session = _from_storage(session_item)
        executions = self.repository._query(
            Key("PK").eq(self._user_pk(identity)) & Key("SK").begins_with(f"{session_item['SK']}#EXEC#"),
            ScanIndexForward=True,
            ConsistentRead=True,
        )
        execution_payloads = []
        for raw_execution in sorted(
            (item for item in executions if item.get("entity_type") == "workout_execution"),
            key=lambda item: int(item.get("prescription_sequence", 0)),
        ):
            execution = _from_storage(raw_execution)
            execution["sets"] = [_from_storage(item) for item in self._set_items(identity, raw_execution)]
            execution_payloads.append({key: value for key, value in execution.items() if key not in {"PK", "SK", "entity_type"}})
        return {
            "session": {key: value for key, value in session.items() if key not in {"PK", "SK", "entity_type"}},
            "executions": execution_payloads,
        }

    def _require_session(self, identity: ServerlessIdentity, session_id: str) -> dict[str, Any]:
        session = self._session_item(identity, session_id)
        if not session:
            raise WorkoutNotFound("Workout session was not found")
        return session

    def _require_in_progress(self, identity: ServerlessIdentity, session_id: str) -> dict[str, Any]:
        session = self._require_session(identity, session_id)
        if session.get("status") != SESSION_STATUS_IN_PROGRESS:
            raise WorkoutConflict("Workout session is not in progress")
        return session

    def _require_execution(self, identity: ServerlessIdentity, session_id: str, execution_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        session = self._require_in_progress(identity, session_id)
        execution = self._execution_item(identity, session, execution_id)
        if not execution:
            raise WorkoutNotFound("Workout execution was not found")
        return session, execution

    def _session_guard(self, session: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "operation": "ConditionCheck",
            "TableName": self.repository.table_name,
            "Key": {"PK": session["PK"], "SK": session["SK"]},
            "ConditionExpression": "#status = :in_progress AND revision = :expected",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {
                ":in_progress": SESSION_STATUS_IN_PROGRESS,
                ":expected": int(session["revision"]),
            },
        }

    def _write_execution(self, session: Mapping[str, Any], **update: Any) -> None:
        self.repository._transact_write([
            self._session_guard(session),
            {"operation": "Update", "TableName": self.repository.table_name, **update},
        ])

    def _now(self) -> str:
        return utc_iso(self.repository._now())

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

    def start_session(self, identity: ServerlessIdentity, day_code: str, *, actual_local_date: date | str) -> dict[str, Any]:
        active = self._active_item(identity)
        if active:
            active_session = self.repository._get({"PK": active["session_pk"], "SK": active["session_sk"]})
            if active_session and active_session.get("status") == SESSION_STATUS_IN_PROGRESS:
                return self._payload(identity, active_session)
            raise WorkoutConflict("An active workout pointer is inconsistent")

        programme = self.repository.get_workout_programme()
        if programme is None:
            raise WorkoutNotFound("Shared workout programme is unavailable")
        requested_day = str(day_code or "").strip().upper()
        day_result = day_response(programme, requested_day)
        if day_result is None:
            raise WorkoutNotFound("Workout programme day was not found")
        day = day_result["day"]
        session_id = str(self.session_id_factory())
        local_date_text = actual_local_date.isoformat() if isinstance(actual_local_date, date) else str(actual_local_date)
        session_sk = self._session_sk(local_date_text, session_id)
        now = self._now()
        session_item = {
            "PK": self._user_pk(identity),
            "SK": session_sk,
            "entity_type": "workout_session",
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
                    "PK": self._user_pk(identity),
                    "SK": self._execution_sk(session_sk, sequence),
                    "entity_type": "workout_execution",
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
        pointer = {
            "PK": self._user_pk(identity),
            "SK": SESSION_ACTIVE_SK,
            "entity_type": "active_workout_pointer",
            "session_id": session_id,
            "session_pk": self._user_pk(identity),
            "session_sk": session_sk,
            "programme_id": session_item["programme_id"],
            "programme_version_id": session_item["programme_version_id"],
            "programme_day_id": requested_day,
            "started_at": now,
            "revision": 1,
            "updated_at": now,
        }
        operations = [
            {"operation": "Put", "TableName": self.repository.table_name, "Item": session_item, "ConditionExpression": "attribute_not_exists(PK)"},
            {"operation": "Put", "TableName": self.repository.table_name, "Item": pointer, "ConditionExpression": "attribute_not_exists(PK)"},
        ]
        operations.append({"operation": "Put", "TableName": self.repository.table_name,
                           "Item": self.locator_item(session_item), "ConditionExpression": "attribute_not_exists(PK)"})
        operations.extend(
            {"operation": "Put", "TableName": self.repository.table_name, "Item": item, "ConditionExpression": "attribute_not_exists(PK)"}
            for item in execution_items
        )
        try:
            self.repository._transact_write(operations)
        except Exception as err:
            if _is_conditional_failure(err):
                active = self._active_item(identity)
                if active:
                    existing = self.repository._get({"PK": active["session_pk"], "SK": active["session_sk"]})
                    if existing and existing.get("status") == SESSION_STATUS_IN_PROGRESS:
                        return self._payload(identity, existing)
                raise WorkoutConflict("Another workout was started concurrently") from err
            raise
        return self._payload(identity, session_item)

    def get_active_session(self, identity: ServerlessIdentity) -> Optional[dict[str, Any]]:
        active = self._active_item(identity)
        if not active:
            return None
        session = self.repository._get({"PK": active["session_pk"], "SK": active["session_sk"]})
        if not session or session.get("status") != SESSION_STATUS_IN_PROGRESS:
            return None
        return self._payload(identity, session)

    def get_session(self, identity: ServerlessIdentity, session_id: str) -> dict[str, Any]:
        session = self._require_session(identity, session_id)
        result = self._payload(identity, session)
        if session.get("status") != SESSION_STATUS_IN_PROGRESS:
            programme = self.repository.get_workout_programme(version_id=session.get("programme_version_id")) or {}
            names = {item["exercise_id"]: item.get("canonical_name", item["exercise_id"]) for item in programme.get("exercises", [])}
            for execution in result["executions"]:
                execution["exercise_name"] = names.get(execution["performed_exercise_id"], execution["performed_exercise_id"])
            result["session"].update(self._summary(self.history_item(session)))
        return result

    @staticmethod
    def _expected_revision(payload: Mapping[str, Any], current: int) -> int:
        if "expected_revision" not in payload:
            raise WorkoutConflict("expected_revision is required")
        expected = WorkoutExecutionRepository._as_int(payload.get("expected_revision"), "expected_revision", minimum=0)
        if expected != current:
            raise WorkoutConflict("Workout state is stale; reload and retry")
        return expected

    def select_exercise(self, identity: ServerlessIdentity, session_id: str, execution_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        session, execution = self._require_execution(identity, session_id, execution_id)
        performed = str(payload.get("performed_exercise_id", "")).strip()
        allowed = {str(item) for item in execution.get("allowed_exercise_ids", [])}
        if performed not in allowed:
            raise InvalidWorkoutInput("performed_exercise_id is not allowed for this prescription")
        if self._set_items(identity, execution):
            raise WorkoutConflict("Exercise choice cannot change after sets are logged")
        expected = self._expected_revision(payload, int(execution.get("revision", 0)))
        if performed == str(execution.get("performed_exercise_id", "")):
            return self._payload(identity, session)
        options = execution.get("option_targets", {}) or {}
        target = options.get(performed, {})
        programme = self.repository.get_workout_programme(version_id=execution.get("programme_version_id"))
        exercise_map = {str(item["exercise_id"]): item for item in (programme or {}).get("exercises", [])}
        exercise = exercise_map.get(performed, {})
        new_revision = expected + 1
        values = {
            ":performed": performed,
            ":reason": str(payload.get("substitution_reason", "") or ""),
            ":execution_type": target.get("execution_type", exercise.get("execution_type", execution.get("execution_type"))),
            ":unilateral_mode": exercise.get("unilateral_mode", execution.get("unilateral_mode", "bilateral")),
            ":loading_convention": exercise.get("loading_convention", execution.get("loading_convention", "none")),
            ":min_reps": target.get("rep_min"),
            ":max_reps": target.get("rep_max"),
            ":min_sets": target.get("set_min", execution.get("prescribed_set_count_min")),
            ":max_sets": target.get("set_max", execution.get("prescribed_set_count_max")),
            ":duration": target.get("duration_seconds"),
            ":empty": "",
            ":new_revision": new_revision,
            ":now": self._now(),
            ":expected": expected,
            ":pending": EXECUTION_STATUS_PENDING,
            ":in_progress": EXECUTION_STATUS_IN_PROGRESS,
        }
        try:
            self._write_execution(
                session,
                Key={"PK": session["PK"], "SK": execution["SK"]},
                UpdateExpression=(
                    "SET performed_exercise_id = :performed, substitution_reason = :reason, "
                    "execution_type = :execution_type, unilateral_mode = :unilateral_mode, "
                    "loading_convention = :loading_convention, prescribed_min_reps = :min_reps, "
                    "prescribed_max_reps = :max_reps, prescribed_set_count_min = :min_sets, "
                    "prescribed_set_count_max = :max_sets, prescribed_duration_seconds = :duration, "
                    "skip_reason = :empty, revision = :new_revision, updated_at = :now"
                ),
                ConditionExpression="revision = :expected AND (#status = :pending OR #status = :in_progress)",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=_to_storage(values),
            )
        except Exception as err:
            if _is_conditional_failure(err):
                raise WorkoutConflict("Workout execution changed; reload and retry") from err
            raise
        return self._payload(identity, session)

    def skip_execution(self, identity: ServerlessIdentity, session_id: str, execution_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        session, execution = self._require_execution(identity, session_id, execution_id)
        reason = str(payload.get("skip_reason", "")).strip() or "intentionally_skipped"
        if reason not in SKIP_REASONS:
            raise InvalidWorkoutInput("skip_reason is invalid")
        if execution.get("status") == EXECUTION_STATUS_SKIPPED and execution.get("skip_reason") == reason and "expected_revision" not in payload:
            return self._payload(identity, session)
        expected = self._expected_revision(payload, int(execution.get("revision", 0)))
        self._update_execution_status(identity, session, execution, EXECUTION_STATUS_SKIPPED, reason, expected)
        return self._payload(identity, session)

    def reset_execution(self, identity: ServerlessIdentity, session_id: str, execution_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        session, execution = self._require_execution(identity, session_id, execution_id)
        expected = self._expected_revision(payload, int(execution.get("revision", 0)))
        self._update_execution_status(identity, session, execution, EXECUTION_STATUS_PENDING, "", expected)
        return self._payload(identity, session)

    def _update_execution_status(self, identity: ServerlessIdentity, session: Mapping[str, Any], execution: Mapping[str, Any], status: str, skip_reason: str, expected: int) -> None:
        try:
            self._write_execution(
                session,
                Key={"PK": session["PK"], "SK": execution["SK"]},
                UpdateExpression="SET #status = :status, skip_reason = :reason, revision = :new_revision, updated_at = :now",
                ConditionExpression="#status = :current AND revision = :expected",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=_to_storage({":status": status, ":reason": skip_reason, ":new_revision": expected + 1, ":now": self._now(), ":current": execution.get("status"), ":expected": expected}),
            )
        except Exception as err:
            if _is_conditional_failure(err):
                raise WorkoutConflict("Workout execution changed; reload and retry") from err
            raise

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

    def put_set(self, identity: ServerlessIdentity, session_id: str, execution_id: str, ordinal: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        session, execution = self._require_execution(identity, session_id, execution_id)
        if execution.get("status") == EXECUTION_STATUS_SKIPPED:
            raise WorkoutConflict("Skipped exercise must be reset before logging sets")
        set_ordinal = self._as_int(ordinal, "set ordinal")
        fields = self._validate_set_payload(execution, payload)
        existing = self.repository._get({"PK": session["PK"], "SK": self._set_sk(str(execution["SK"]), set_ordinal)})
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
            "PK": session["PK"],
            "SK": self._set_sk(str(execution["SK"]), set_ordinal),
            "entity_type": "workout_set",
            "set_id": f"{execution_id}:{set_ordinal:03d}",
            "session_id": session_id,
            "execution_id": execution_id,
            "set_ordinal": set_ordinal,
            **fields,
            "revision": current_set_revision + 1,
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
        }
        set_operation = {
            "operation": "Put",
            "TableName": self.repository.table_name,
            "Item": item,
            "ConditionExpression": "attribute_not_exists(PK)" if not existing else "revision = :expected_set",
        }
        if existing:
            set_operation["ExpressionAttributeValues"] = {":expected_set": expected_set}
        execution_operation = {
            "operation": "Update",
            "TableName": self.repository.table_name,
            "Key": {"PK": session["PK"], "SK": execution["SK"]},
            "UpdateExpression": "SET #status = :status, revision = :execution_new_revision, updated_at = :now",
            "ConditionExpression": "revision = :execution_expected AND (#status = :pending OR #status = :in_progress)",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {
                ":status": EXECUTION_STATUS_IN_PROGRESS,
                ":execution_new_revision": execution_expected + 1,
                ":now": now,
                ":execution_expected": execution_expected,
                ":pending": EXECUTION_STATUS_PENDING,
                ":in_progress": EXECUTION_STATUS_IN_PROGRESS,
            },
        }
        try:
            self.repository._transact_write([self._session_guard(session), set_operation, execution_operation])
        except Exception as err:
            if _is_conditional_failure(err):
                raise WorkoutConflict("Workout set changed; reload and retry") from err
            raise
        return self._payload(identity, session)

    def skip_set(self, identity: ServerlessIdentity, session_id: str, execution_id: str, ordinal: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        payload_with_status = dict(payload)
        payload_with_status["status"] = SET_STATUS_SKIPPED
        return self.put_set(identity, session_id, execution_id, ordinal, payload_with_status)

    def complete_session(self, identity: ServerlessIdentity, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        session = self._require_in_progress(identity, session_id)
        expected = self._expected_revision(payload, int(session.get("revision", 0)))
        executions = self.repository._query(
            Key("PK").eq(self._user_pk(identity)) & Key("SK").begins_with(f"{session['SK']}#EXEC#"),
            ScanIndexForward=True,
            ConsistentRead=True,
        )
        blockers = []
        for execution in sorted(
            (item for item in executions if item.get("entity_type") == "workout_execution"),
            key=lambda item: int(item.get("prescription_sequence", 0)),
        ):
            if execution.get("status") == EXECUTION_STATUS_SKIPPED:
                continue
            sets = self._set_items(identity, execution)
            minimum_sets = max(1, int(execution.get("prescribed_set_count_min") or 1))
            if len(self._resolved_working_sets(sets)) < minimum_sets:
                blockers.append(str(execution.get("prescription_sequence", "exercise")))
        if blockers:
            exercises = ", ".join(blockers)
            raise WorkoutConflict(f"Log or skip every exercise before submitting (incomplete: {exercises})")

        now = self._now()
        operations = [
            {
                "operation": "Update",
                "TableName": self.repository.table_name,
                "Key": {"PK": session["PK"], "SK": session["SK"]},
                "UpdateExpression": "SET #status = :completed, completed_at = :completed_at, revision = :new_revision, updated_at = :now",
                "ConditionExpression": "#status = :in_progress AND revision = :expected",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {
                    ":completed": SESSION_STATUS_COMPLETED,
                    ":completed_at": now,
                    ":new_revision": expected + 1,
                    ":now": now,
                    ":in_progress": SESSION_STATUS_IN_PROGRESS,
                    ":expected": expected,
                },
            },
        ]
        operations.append(self._history_put(session, SESSION_STATUS_COMPLETED, now))
        for execution in executions:
            if execution.get("entity_type") != "workout_execution":
                continue
            execution_revision = int(execution.get("revision", 0))
            if execution.get("status") == EXECUTION_STATUS_SKIPPED:
                operations.append({
                    "operation": "ConditionCheck",
                    "TableName": self.repository.table_name,
                    "Key": {"PK": session["PK"], "SK": execution["SK"]},
                    "ConditionExpression": "#status = :skipped AND revision = :expected",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {":skipped": EXECUTION_STATUS_SKIPPED, ":expected": execution_revision},
                })
                continue
            operations.append(
                {
                    "operation": "Update",
                    "TableName": self.repository.table_name,
                    "Key": {"PK": session["PK"], "SK": execution["SK"]},
                    "UpdateExpression": "SET #status = :completed, revision = :new_revision, updated_at = :now",
                    "ConditionExpression": "#status = :in_progress AND revision = :expected",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":completed": EXECUTION_STATUS_COMPLETED,
                        ":new_revision": execution_revision + 1,
                        ":now": now,
                        ":in_progress": EXECUTION_STATUS_IN_PROGRESS,
                        ":expected": execution_revision,
                    },
                }
            )
        operations.append(
            {
                "operation": "Delete",
                "TableName": self.repository.table_name,
                "Key": {"PK": identity.pk, "SK": SESSION_ACTIVE_SK},
                "ConditionExpression": "session_id = :session_id",
                "ExpressionAttributeValues": {":session_id": session["session_id"]},
            }
        )
        try:
            self.repository._transact_write(operations)
        except Exception as err:
            if _is_conditional_failure(err):
                raise WorkoutConflict("Workout session changed; reload and retry") from err
            raise
        updated = self.repository._get({"PK": session["PK"], "SK": session["SK"]})
        return self._payload(identity, updated or session)

    def cancel_session(self, identity: ServerlessIdentity, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        session = self._require_in_progress(identity, session_id)
        expected = self._expected_revision(payload, int(session.get("revision", 0)))
        now = self._now()
        try:
            self.repository._transact_write([
                self._history_put(session, SESSION_STATUS_CANCELLED, now),
                {
                    "operation": "Update",
                    "TableName": self.repository.table_name,
                    "Key": {"PK": session["PK"], "SK": session["SK"]},
                    "UpdateExpression": "SET #status = :cancelled, completed_at = :completed_at, revision = :new_revision, updated_at = :now",
                    "ConditionExpression": "#status = :in_progress AND revision = :expected",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {":cancelled": SESSION_STATUS_CANCELLED, ":completed_at": now, ":new_revision": expected + 1, ":now": now, ":in_progress": SESSION_STATUS_IN_PROGRESS, ":expected": expected},
                },
                {
                    "operation": "Delete",
                    "TableName": self.repository.table_name,
                    "Key": {"PK": session["PK"], "SK": SESSION_ACTIVE_SK},
                    "ConditionExpression": "session_id = :session_id",
                    "ExpressionAttributeValues": {":session_id": session["session_id"]},
                },
            ])
        except Exception as err:
            if _is_conditional_failure(err):
                raise WorkoutConflict("Workout session changed; reload and retry") from err
            raise
        updated = self.repository._get({"PK": session["PK"], "SK": session["SK"]})
        return self._payload(identity, updated or session)
