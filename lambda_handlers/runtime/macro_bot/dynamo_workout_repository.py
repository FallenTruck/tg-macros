"""Workout keys, record mapping and atomic DynamoDB persistence."""
from __future__ import annotations
import base64
import binascii
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional
from boto3.dynamodb.conditions import Key
from .dynamo_store import DynamoDBStore, from_storage as _from_storage, is_conditional_failure as _is_conditional_failure
from .workout_types import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_IN_PROGRESS,
    EXECUTION_STATUS_PENDING,
    EXECUTION_STATUS_SKIPPED,
    InvalidWorkoutInput,
    SESSION_STATUS_CANCELLED,
    SESSION_STATUS_COMPLETED,
    SESSION_STATUS_IN_PROGRESS,
    WorkoutConflict,
)
from .workout_repository import Record, WorkoutIdentity

SESSION_ACTIVE_SK = "WORKOUT#ACTIVE"

class DynamoWorkoutRepository:
    def __init__(self, store: DynamoDBStore):
        self.store = store

    @staticmethod
    def _record(item):
        if item is None:
            return None
        return {k: v for k, v in _from_storage(item).items() if k not in {"PK", "SK", "entity_type"}}

    def _session_record(self, identity, session):
        return {**session, "PK": identity.pk, "SK": self._session_sk(session["actual_local_date"], session["session_id"]), "entity_type": "workout_session"}

    def _execution_record(self, session, execution):
        return {**execution, "PK": session["PK"], "SK": self._execution_sk(session["SK"], int(execution["prescription_sequence"])), "entity_type": "workout_execution"}

    def get_active_session(self, identity: WorkoutIdentity) -> tuple[bool, Optional[dict[str, Any]]]:
        active = self._active_item(identity)
        if active is None:
            return False, None
        return True, self._record(self.store.get_item({"PK": active["session_pk"], "SK": active["session_sk"]}))

    def get_session(self, identity: WorkoutIdentity, session_id: str) -> Optional[dict[str, Any]]:
        return self._record(self._session_item(identity, session_id))

    def get_executions(self, identity: WorkoutIdentity, session: Record) -> list[dict[str, Any]]:
        session = self._session_record(identity, session)
        records = self.store.query(Key("PK").eq(identity.pk) & Key("SK").begins_with(f"{session['SK']}#EXEC#"), ScanIndexForward=True, ConsistentRead=True)
        return [self._record(item) for item in records if item.get("entity_type") == "workout_execution"]

    def get_execution(self, identity: WorkoutIdentity, session: Record, execution_id: str) -> Optional[dict[str, Any]]:
        return next((item for item in self.get_executions(identity, session) if str(item.get("execution_id")) == str(execution_id)), None)

    def get_sets(self, identity: WorkoutIdentity, session: Record, execution: Record) -> list[dict[str, Any]]:
        raw_session = self._session_record(identity, session)
        # Nested set responses historically expose these storage fields. Preserve
        # that public contract while keeping their construction out of the service.
        return [_from_storage(item) for item in self._set_items(identity, self._execution_record(raw_session, execution))]

    def get_set(self, identity: WorkoutIdentity, session: Record, execution: Record, ordinal: int) -> Optional[dict[str, Any]]:
        raw_session = self._session_record(identity, session)
        raw_execution = self._execution_record(raw_session, execution)
        return self._record(self.store.get_item({"PK": identity.pk, "SK": self._set_sk(raw_execution["SK"], ordinal)}))

    @staticmethod
    def _user_pk(identity: WorkoutIdentity) -> str:
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

    def _active_item(self, identity: WorkoutIdentity) -> Optional[dict[str, Any]]:
        return self.store.get_item({"PK": self._user_pk(identity), "SK": SESSION_ACTIVE_SK})

    def _session_item(self, identity: WorkoutIdentity, session_id: str) -> Optional[dict[str, Any]]:
        active = self._active_item(identity)
        if active and str(active.get("session_id")) == str(session_id):
            item = self.store.get_item({"PK": str(active["session_pk"]), "SK": str(active["session_sk"])})
            if item:
                return item
        locator = self.store.get_item({"PK": identity.pk, "SK": f"WORKOUT_SESSION#{session_id}"})
        if not locator:
            return None
        return self.store.get_item({"PK": identity.pk, "SK": locator["session_sk"]})

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
        return {"operation": "Put", "TableName": self.store.table_name,
                "Item": self.history_item({**session, "status": status, "completed_at": now}),
                "ConditionExpression": "attribute_not_exists(PK)"}

    @staticmethod
    def _encode_cursor(pk: str, sk: str) -> str:
        return base64.urlsafe_b64encode(json.dumps({"v": 1, "pk": pk, "sk": sk},
                                                  sort_keys=True, separators=(",", ":")).encode()).decode()

    def _set_items(self, identity: WorkoutIdentity, execution_item: Mapping[str, Any]) -> list[dict[str, Any]]:
        prefix = f"{execution_item['SK']}#SET#"
        records = self.store.query(
            Key("PK").eq(self._user_pk(identity)) & Key("SK").begins_with(prefix),
            ScanIndexForward=True,
            ConsistentRead=True,
        )
        return sorted(
            (item for item in records if item.get("entity_type") == "workout_set"),
            key=lambda item: int(item.get("set_ordinal", 0)),
        )

    def _session_guard(self, session: Mapping[str, Any]) -> dict[str, Any]:
        """Fence every child mutation against concurrent completion/cancellation."""
        return {
            "operation": "ConditionCheck",
            "TableName": self.store.table_name,
            "Key": {"PK": session["PK"], "SK": session["SK"]},
            "ConditionExpression": "#status = :in_progress AND revision = :expected",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {
                ":in_progress": SESSION_STATUS_IN_PROGRESS,
                ":expected": int(session["revision"]),
            },
        }

    def list_history(self, identity: WorkoutIdentity, *, limit: Any = 20,
                             cursor: Optional[str] = None) -> dict[str, Any]:
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
        sessions = []
        while True:
            # Limit evaluated summaries to the remaining slots so no completed
            # session is discarded when advancing the DynamoDB cursor.
            kwargs["Limit"] = int(limit) - len(sessions)
            page = self.store.query_page(**kwargs)
            sessions.extend(self._record(item) for item in page.get("Items", [])
                            if item.get("status") == SESSION_STATUS_COMPLETED)
            last = page.get("LastEvaluatedKey")
            if not last or len(sessions) >= int(limit):
                break
            kwargs["ExclusiveStartKey"] = last
        return {"sessions": sessions,
                "next_cursor": self._encode_cursor(last["PK"], last["SK"]) if last else None}

    def create_session(self, identity: WorkoutIdentity, session: Record, executions: list[dict[str, Any]]) -> None:
        session_item = self._session_record(identity, session)
        execution_items = [self._execution_record(session_item, item) for item in executions]
        pointer = {
            "PK": self._user_pk(identity),
            "SK": SESSION_ACTIVE_SK,
            "entity_type": "active_workout_pointer",
            "session_id": session_item["session_id"],
            "session_pk": self._user_pk(identity),
            "session_sk": session_item["SK"],
            "programme_id": session_item["programme_id"],
            "programme_version_id": session_item["programme_version_id"],
            "programme_day_id": session_item["programme_day_id"],
            "started_at": session_item["started_at"],
            "revision": 1,
            "updated_at": session_item["started_at"],
        }
        operations = [
            {"operation": "Put", "TableName": self.store.table_name, "Item": session_item, "ConditionExpression": "attribute_not_exists(PK)"},
            {"operation": "Put", "TableName": self.store.table_name, "Item": pointer, "ConditionExpression": "attribute_not_exists(PK)"},
        ]
        operations.append({"operation": "Put", "TableName": self.store.table_name,
                           "Item": self.locator_item(session_item), "ConditionExpression": "attribute_not_exists(PK)"})
        operations.extend(
            {"operation": "Put", "TableName": self.store.table_name, "Item": item, "ConditionExpression": "attribute_not_exists(PK)"}
            for item in execution_items
        )
        self._commit(operations, "Another workout was started concurrently")

    def update_execution(self, identity: WorkoutIdentity, session: Record, execution: Record, fields: Record, expected: int, *, selectable: bool=False) -> None:
        session = self._session_record(identity, session)
        execution = self._execution_record(session, execution)
        names = {f"#f{i}": name for i, name in enumerate(fields)}
        values = {f":v{i}": value for i, value in enumerate(fields.values())}
        expression = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
        names["#status"] = "status"
        values[":expected"] = expected
        if selectable:
            condition = "revision = :expected AND (#status = :pending OR #status = :in_progress)"
            values.update({":pending": EXECUTION_STATUS_PENDING, ":in_progress": EXECUTION_STATUS_IN_PROGRESS})
        else:
            condition = "#status = :current AND revision = :expected"
            values[":current"] = execution.get("status")
        self._commit([self._session_guard(session), {
            "operation": "Update", "TableName": self.store.table_name,
            "Key": {"PK": session["PK"], "SK": execution["SK"]},
            "UpdateExpression": expression, "ConditionExpression": condition,
            "ExpressionAttributeNames": names, "ExpressionAttributeValues": values,
        }], "Workout execution changed; reload and retry")

    def save_set(self, identity: WorkoutIdentity, session: Record, execution: Record, item: Record, expected_set: int, execution_expected: int, exists: bool) -> None:
        session = self._session_record(identity, session)
        execution = self._execution_record(session, execution)
        item = {**item, "PK": session["PK"], "SK": self._set_sk(execution["SK"], item["set_ordinal"]), "entity_type": "workout_set"}
        now = item["updated_at"]
        set_operation = {
            "operation": "Put",
            "TableName": self.store.table_name,
            "Item": item,
            "ConditionExpression": "attribute_not_exists(PK)" if not exists else "revision = :expected_set",
        }
        if exists:
            set_operation["ExpressionAttributeValues"] = {":expected_set": expected_set}
        execution_operation = {
            "operation": "Update",
            "TableName": self.store.table_name,
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
        self._commit([self._session_guard(session), set_operation, execution_operation], 'Workout set changed; reload and retry')

    def complete_session(self, identity: WorkoutIdentity, session: Record, executions: list[dict[str, Any]], expected: int, now: str) -> None:
        session = self._session_record(identity, session)
        executions = [self._execution_record(session, item) for item in executions]
        operations = [
            {
                "operation": "Update",
                "TableName": self.store.table_name,
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
            execution_revision = int(execution.get("revision", 0))
            if execution.get("status") == EXECUTION_STATUS_SKIPPED:
                # A reset must invalidate the completion decision even without a write to this execution.
                operations.append({
                    "operation": "ConditionCheck",
                    "TableName": self.store.table_name,
                    "Key": {"PK": session["PK"], "SK": execution["SK"]},
                    "ConditionExpression": "#status = :skipped AND revision = :expected",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {":skipped": EXECUTION_STATUS_SKIPPED, ":expected": execution_revision},
                })
                continue
            operations.append(
                {
                    "operation": "Update",
                    "TableName": self.store.table_name,
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
                "TableName": self.store.table_name,
                "Key": {"PK": identity.pk, "SK": SESSION_ACTIVE_SK},
                "ConditionExpression": "session_id = :session_id",
                "ExpressionAttributeValues": {":session_id": session["session_id"]},
            }
        )
        self._commit(operations, 'Workout session changed; reload and retry')

    def cancel_session(self, identity: WorkoutIdentity, session: Record, expected: int, now: str) -> None:
        session = self._session_record(identity, session)
        self._commit([
                self._history_put(session, SESSION_STATUS_CANCELLED, now),
                {
                    "operation": "Update",
                    "TableName": self.store.table_name,
                    "Key": {"PK": session["PK"], "SK": session["SK"]},
                    "UpdateExpression": "SET #status = :cancelled, completed_at = :completed_at, revision = :new_revision, updated_at = :now",
                    "ConditionExpression": "#status = :in_progress AND revision = :expected",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {":cancelled": SESSION_STATUS_CANCELLED, ":completed_at": now, ":new_revision": expected + 1, ":now": now, ":in_progress": SESSION_STATUS_IN_PROGRESS, ":expected": expected},
                },
                {
                    "operation": "Delete",
                    "TableName": self.store.table_name,
                    "Key": {"PK": session["PK"], "SK": SESSION_ACTIVE_SK},
                    "ConditionExpression": "session_id = :session_id",
                    "ExpressionAttributeValues": {":session_id": session["session_id"]},
                },
            ], 'Workout session changed; reload and retry')

    def _commit(self, operations, message):
        try:
            self.store.transact_write(operations)
        except Exception as err:
            if _is_conditional_failure(err):
                raise WorkoutConflict(message) from err
            raise
