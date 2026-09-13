"""DynamoDB persistence for shared immutable workout programmes and publication."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from boto3.dynamodb.conditions import Key

from .dynamo_store import DynamoDBStore, from_storage, is_conditional_failure
from .programme_repository import ProgrammeSeedConflict
from .workout_programme import (
    CORE_OPTIONS_VERSION_ID,
    INITIAL_VERSION_ID,
    PROGRAMME_PK,
    day_response,
    core_options_programme_records,
    initial_programme_records,
    programme_response,
)


class DynamoProgrammeRepository:
    def __init__(self, store: DynamoDBStore, *, now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.store = store
        self.now_fn = now_fn

    def _now(self) -> str:
        moment = self.now_fn()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _programme_records(self) -> list[dict[str, Any]]:
        records = self.store.query(Key("PK").eq(PROGRAMME_PK))
        records.extend(self.store.query(Key("PK").eq("CATALOG#EXERCISES")))
        return [from_storage(item) for item in records]

    def get_programme(self, version_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Return a shared programme assembled from immutable records."""

        records = self._programme_records()
        metadata = next((item for item in records if item.get("entity_type") == "workout_programme"), None)
        if metadata is None:
            return None
        selected_version = str(version_id or metadata.get("active_version_id") or "").strip()
        if not selected_version:
            return None
        selected = [
            item
            for item in records
            if item.get("entity_type") in {"workout_programme", "workout_programme_version", "workout_programme_day", "programme_prescription", "exercise"}
            and (item.get("entity_type") in {"workout_programme", "exercise"} or item.get("version_id") == selected_version)
        ]
        result = programme_response(selected, version_id=selected_version)
        return result if result.get("version") else None

    def get_programme_day(self, day_code: str, version_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        programme = self.get_programme(version_id=version_id)
        if programme is None:
            return None
        return day_response(programme, day_code)

    def seed_workout_programme(self, *, dry_run: bool = False) -> dict[str, int]:
        """Reconcile the deterministic initial programme without overwriting."""

        records = initial_programme_records()
        existing: dict[tuple[str, str], Optional[dict[str, Any]]] = {}
        for desired in records:
            key = (str(desired["PK"]), str(desired["SK"]))
            current = self.store.get_item({"PK": key[0], "SK": key[1]})
            existing[key] = from_storage(current) if current else None
            if current is not None and from_storage(current) != desired:
                raise ProgrammeSeedConflict(f"conflicting programme record: {key[0]} / {key[1]}")
        if dry_run:
            return {"created": 0, "existing": sum(value is not None for value in existing.values()), "would_create": sum(value is None for value in existing.values()), "records": len(records)}
        created = 0
        already_existing = 0
        for desired in records:
            key = (str(desired["PK"]), str(desired["SK"]))
            if existing[key] is not None:
                already_existing += 1
                continue
            try:
                self.store.put_item(desired, ConditionExpression="attribute_not_exists(PK)")
                created += 1
            except Exception as err:
                if not is_conditional_failure(err):
                    raise
                current = self.store.get_item({"PK": key[0], "SK": key[1]})
                if current is None or from_storage(current) != desired:
                    raise ProgrammeSeedConflict(f"conflicting programme record after concurrent write: {key[0]} / {key[1]}") from err
                already_existing += 1
        return {"created": created, "existing": already_existing, "would_create": 0, "records": len(records)}

    def publish_core_options_programme(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Atomically publish additive core choices without rewriting old versions."""
        pointers = [self.store.get_item({"PK": PROGRAMME_PK, "SK": sk}) for sk in ("META", "ACTIVE")]
        if any(not item for item in pointers):
            raise ProgrammeSeedConflict("Seed the initial programme before publishing core choices")
        versions = {str(item.get("active_version_id")) for item in pointers}
        if len(versions) != 1 or not versions <= {INITIAL_VERSION_ID, CORE_OPTIONS_VERSION_ID}:
            raise ProgrammeSeedConflict("Unexpected active programme version")
        expected_version = next(iter(versions))
        operations = []
        for desired in core_options_programme_records():
            current = self.store.get_item({"PK": desired["PK"], "SK": desired["SK"]})
            if current is not None:
                if from_storage(current) != desired:
                    raise ProgrammeSeedConflict("Core programme publication conflicts with an existing record")
                continue
            operations.append({"operation": "Put", "TableName": self.store.table_name, "Item": desired,
                               "ConditionExpression": "attribute_not_exists(PK)"})
        created = len(operations)
        if expected_version != CORE_OPTIONS_VERSION_ID:
            for current in pointers:
                desired = from_storage(current)
                desired.update(active_version_id=CORE_OPTIONS_VERSION_ID, updated_at=self._now())
                operations.append({"operation": "Put", "TableName": self.store.table_name, "Item": desired,
                                   "ConditionExpression": "active_version_id = :version",
                                   "ExpressionAttributeValues": {":version": expected_version}})
        if not dry_run and operations:
            try:
                self.store.transact_write(operations)
            except Exception as err:
                if is_conditional_failure(err):
                    raise ProgrammeSeedConflict("Programme changed during publication; reload and retry") from err
                raise
        return {"version_id": CORE_OPTIONS_VERSION_ID, "created": 0 if dry_run else created,
                "would_create": created, "activate": expected_version != CORE_OPTIONS_VERSION_ID,
                "dry_run": dry_run}
