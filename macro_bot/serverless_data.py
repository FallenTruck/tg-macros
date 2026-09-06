"""DynamoDB-backed nutrition persistence for serverless runtimes.

The table is intentionally single-table and user-partitioned.  Every method
accepts an internal application user id, which is obtained only after
resolving the Telegram identity record.  Legacy CSV/JSON repositories are not
used here and no method writes to those files.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeSerializer

from .serverless_auth import browser_session_token_hash, normalize_web_username, web_credential_key
from .models import (
    DailyMacroSummary,
    LoggedMealRow,
    MacroTotal,
    MealEstimate,
    PendingMealAction,
    UserProfile,
)
from .workout_programme import (
    CORE_OPTIONS_VERSION_ID,
    INITIAL_VERSION_ID,
    PROGRAMME_ID,
    PROGRAMME_PK,
    day_response,
    core_options_programme_records,
    initial_programme_records,
    programme_response,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Asia/Singapore"
WORKFLOW_TTL_SECONDS = 30 * 60
ACTION_TTL_SECONDS = 60 * 60
# Keep finalized action records around long enough for the expiry sweep to
# observe them before DynamoDB's asynchronous TTL deletion.
ACTION_RECORD_RETENTION_SECONDS = 7 * 24 * 60 * 60
# Every authenticated request rechecks the launch context, including set saves.
# Match the one-hour Telegram authentication window so workouts are not cut short.
MINI_APP_LAUNCH_TTL_SECONDS = 60 * 60
BROWSER_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60


class DataError(RuntimeError):
    """Base class for durable application data errors."""


class IdentityCreationRace(DataError):
    pass


class ActionNotFound(DataError):
    pass


class ActionExpired(DataError):
    pass


class ActionFinalized(DataError):
    pass


class ProgrammeSeedConflict(DataError):
    """Raised when a deterministic programme key contains different data."""

    pass


class WebCredentialExists(DataError):
    """Raised when provisioning would replace a credential without consent."""

    pass


@dataclass(frozen=True)
class ServerlessIdentity:
    telegram_user_id: int
    user_id: str
    username: str
    display_name: str
    created_at: str
    updated_at: str
    identity_key: str = ""

    @property
    def pk(self) -> str:
        return f"USER#{self.user_id}"

    @property
    def identity_pk(self) -> str:
        return self.identity_key or f"IDENTITY#TELEGRAM#{self.telegram_user_id}"


@dataclass(frozen=True)
class StoredMeal:
    meal_id: str
    user_id: str
    telegram_user_id: int
    eaten_at: str
    caption: str
    username: str
    status: str
    original_estimate: MealEstimate
    final_estimate: Optional[MealEstimate]
    adjustment_factor: float
    message_id: Optional[int]
    created_at: str
    updated_at: str
    confirmed_at: Optional[str] = None  # Unknown for legacy rows; never infer from updated_at.

    @property
    def macros(self) -> MacroTotal:
        return (self.final_estimate or self.original_estimate).total_best

    @property
    def entry_delay_minutes(self) -> Optional[int]:
        if not self.confirmed_at:
            return None
        return max(0, int((parse_utc(self.confirmed_at) - parse_utc(self.eaten_at)).total_seconds() // 60))


@dataclass(frozen=True)
class FinalizeResult:
    status: str
    action: PendingMealAction
    meal: Optional[StoredMeal]
    duplicate: bool = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: Optional[datetime] = None) -> str:
    moment = value or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def epoch_seconds(value: Optional[datetime] = None) -> int:
    return int((value or utc_now()).timestamp())


def local_day_utc_bounds(target_date: date, timezone_name: str = DEFAULT_TIMEZONE) -> tuple[str, str]:
    """Return UTC ISO bounds for a user's local calendar day."""

    try:
        tz = ZoneInfo(timezone_name or DEFAULT_TIMEZONE)
    except Exception as err:
        raise ValueError(f"Invalid timezone: {timezone_name}") from err
    start_local = datetime.combine(target_date, time.min, tzinfo=tz)
    # Reconstruct midnight for the next calendar date so DST transitions do
    # not turn a local calendar day into a fixed 24-hour UTC interval.
    end_local = datetime.combine(target_date + timedelta(days=1), time.min, tzinfo=tz)
    return utc_iso(start_local), utc_iso(end_local)


def _to_storage(value: Any) -> Any:
    """Convert JSON-like values to values accepted by the DynamoDB resource."""

    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _to_storage(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_storage(item) for item in value]
    return value


def _from_storage(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value) if value % 1 else int(value)
    if isinstance(value, Mapping):
        return {str(key): _from_storage(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_from_storage(item) for item in value]
    return value


def _is_conditional_failure(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if isinstance(response, Mapping):
        code = response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            return True
    return type(error).__name__ in {
        "ConditionalCheckFailedException",
        "TransactionCanceledException",
    }


def _estimate_payload(estimate: MealEstimate) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "meal_name": estimate.meal_name,
        "calories": estimate.calories,
        "protein_g": estimate.protein_g,
        "carbs_g": estimate.carbs_g,
        "fat_g": estimate.fat_g,
        "total_best": estimate.total_best.to_payload(),
        "confidence": estimate.confidence,
        "notes": estimate.notes,
        "items": [
            {
                "name": item.name,
                "portion_g": item.portion_g,
                "assumptions": item.assumptions,
                "assumption_categories": dict(item.assumption_categories),
                "calories": item.calories,
                "protein_g": item.protein_g,
                "carbs_g": item.carbs_g,
                "fat_g": item.fat_g,
                "evidence": item.evidence,
                **({"portion_low_g": item.portion_low_g} if item.portion_low_g is not None else {}),
                **({"portion_high_g": item.portion_high_g} if item.portion_high_g is not None else {}),
                **({"identification_confidence": item.identification_confidence} if item.identification_confidence is not None else {}),
                **({"portion_confidence": item.portion_confidence} if item.portion_confidence is not None else {}),
            }
            for item in estimate.items
        ],
        "variance_drivers": list(estimate.variance_drivers),
        "item_breakdown_complete": estimate.item_breakdown_complete,
        "estimator_version": estimate.estimator_version,
        "reconciliation_status": estimate.reconciliation_status,
        "canonical_total": estimate.total_best.to_payload(),
    }
    if estimate.total_low is not None:
        payload["total_low"] = estimate.total_low.to_payload()
    if estimate.total_high is not None:
        payload["total_high"] = estimate.total_high.to_payload()
    if estimate.metrics_event_id:
        payload["metrics_event_id"] = estimate.metrics_event_id
    if estimate.identification_confidence is not None:
        payload["identification_confidence"] = estimate.identification_confidence
    if estimate.portion_confidence is not None:
        payload["portion_confidence"] = estimate.portion_confidence
    if estimate.macro_confidence is not None:
        payload["macro_confidence"] = estimate.macro_confidence
    if estimate.model_reported_total is not None:
        payload["model_reported_total"] = estimate.model_reported_total.to_payload()
    if estimate.item_derived_total is not None:
        payload["item_derived_total"] = estimate.item_derived_total.to_payload()
    if estimate.follow_up_question:
        payload["follow_up_question"] = estimate.follow_up_question
    return payload


def _estimate_from_payload(payload: Mapping[str, Any]) -> MealEstimate:
    return MealEstimate.from_api_payload(dict(payload))


def _target_payload(target: MacroTotal) -> dict[str, float]:
    return target.to_payload()


class DynamoNutritionRepository:
    """Repository for all new AWS nutrition records."""

    def __init__(
        self,
        table: Any = None,
        *,
        table_name: Optional[str] = None,
        client: Any = None,
        now_fn: Any = utc_now,
        identity_id_factory: Any = lambda: uuid.uuid4().hex,
        token_factory: Any = lambda: secrets.token_urlsafe(18),
        session_token_factory: Any = lambda: secrets.token_urlsafe(32),
    ):
        if table is None:
            import boto3

            resource = boto3.resource("dynamodb")
            table = resource.Table(table_name or "")
        self.table = table
        self.table_name = table_name or getattr(table, "name", None) or getattr(table, "table_name", None)
        if not self.table_name:
            raise ValueError("DynamoDB table name is required")
        if client is not None:
            self.client = client
        else:
            resource_client = getattr(getattr(table, "meta", None), "client", None)
            # boto3 resource clients apply the resource serializer to the
            # already-serialized transaction maps. Use a standalone low-level
            # client for real DynamoDB resources while retaining injected fake
            # clients for unit tests.
            if resource_client is not None and type(resource_client).__module__.startswith("botocore."):
                import boto3

                self.client = boto3.client("dynamodb")
            else:
                self.client = resource_client
        self.now_fn = now_fn
        self.identity_id_factory = identity_id_factory
        self.token_factory = token_factory
        self.session_token_factory = session_token_factory

    def _now(self) -> datetime:
        value = self.now_fn()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _get(self, key: Mapping[str, str]) -> Optional[dict[str, Any]]:
        try:
            result = self.table.get_item(Key=dict(key), ConsistentRead=True)
        except TypeError:
            result = self.table.get_item(Key=dict(key))
        item = result.get("Item") if isinstance(result, Mapping) else None
        return dict(item) if item else None

    def _query(self, key_expression: Any, **kwargs: Any) -> list[dict[str, Any]]:
        query_kwargs = dict(kwargs)
        results: list[dict[str, Any]] = []
        requested_limit = query_kwargs.get("Limit")
        while True:
            result = self.table.query(KeyConditionExpression=key_expression, **query_kwargs)
            if isinstance(result, Mapping):
                results.extend(dict(item) for item in result.get("Items", []))
                last_key = result.get("LastEvaluatedKey")
            else:
                last_key = None
            if requested_limit is not None and len(results) >= int(requested_limit):
                return results[: int(requested_limit)]
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
            if requested_limit is not None:
                query_kwargs["Limit"] = max(1, int(requested_limit) - len(results))
        return results

    def _transact_write(self, operations: Sequence[dict[str, Any]]) -> None:
        low_level_operations: list[dict[str, Any]] = []
        serializer = TypeSerializer()
        for operation in operations:
            operation_name = str(operation.get("operation", "Put"))
            # TableName belongs on every low-level transaction operation, not
            # on the TransactWriteItems request itself.
            converted = {key: value for key, value in operation.items() if key != "operation"}
            if "Item" in converted:
                converted["Item"] = {
                    key: serializer.serialize(_to_storage(value))
                    for key, value in converted["Item"].items()
                }
            if "Key" in converted:
                converted["Key"] = {
                    key: serializer.serialize(_to_storage(value))
                    for key, value in converted["Key"].items()
                }
            if "ExpressionAttributeValues" in converted:
                converted["ExpressionAttributeValues"] = {
                    key: serializer.serialize(_to_storage(value))
                    for key, value in converted["ExpressionAttributeValues"].items()
                }
            low_level_operations.append({operation_name: converted})

        if self.client is not None and hasattr(self.client, "transact_write_items"):
            self.client.transact_write_items(TransactItems=low_level_operations)
            return
        if hasattr(self.table, "transact_write_items"):
            self.table.transact_write_items(TransactItems=low_level_operations)
            return
        raise RuntimeError("DynamoDB transaction client is not configured")

    # ---- Shared workout programme --------------------------------------------

    def _programme_records(self) -> list[dict[str, Any]]:
        records = self._query(Key("PK").eq(PROGRAMME_PK))
        records.extend(self._query(Key("PK").eq("CATALOG#EXERCISES")))
        return [_from_storage(item) for item in records]

    def get_workout_programme(self, version_id: Optional[str] = None) -> Optional[dict[str, Any]]:
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

    def get_workout_programme_day(self, day_code: str, version_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        programme = self.get_workout_programme(version_id=version_id)
        if programme is None:
            return None
        return day_response(programme, day_code)

    def seed_workout_programme(self, *, dry_run: bool = False) -> dict[str, int]:
        """Reconcile the deterministic initial programme without overwriting."""

        records = initial_programme_records()
        existing: dict[tuple[str, str], Optional[dict[str, Any]]] = {}
        for desired in records:
            key = (str(desired["PK"]), str(desired["SK"]))
            current = self._get({"PK": key[0], "SK": key[1]})
            existing[key] = _from_storage(current) if current else None
            if current is not None and _from_storage(current) != desired:
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
                self.table.put_item(Item=_to_storage(desired), ConditionExpression="attribute_not_exists(PK)")
                created += 1
            except Exception as err:
                if not _is_conditional_failure(err):
                    raise
                current = self._get({"PK": key[0], "SK": key[1]})
                if current is None or _from_storage(current) != desired:
                    raise ProgrammeSeedConflict(f"conflicting programme record after concurrent write: {key[0]} / {key[1]}") from err
                already_existing += 1
        return {"created": created, "existing": already_existing, "would_create": 0, "records": len(records)}

    def publish_core_options_programme(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Atomically publish additive core choices without rewriting old versions."""
        pointers = [self._get({"PK": PROGRAMME_PK, "SK": sk}) for sk in ("META", "ACTIVE")]
        if any(not item for item in pointers):
            raise ProgrammeSeedConflict("Seed the initial programme before publishing core choices")
        versions = {str(item.get("active_version_id")) for item in pointers}
        if len(versions) != 1 or not versions <= {INITIAL_VERSION_ID, CORE_OPTIONS_VERSION_ID}:
            raise ProgrammeSeedConflict("Unexpected active programme version")
        expected_version = next(iter(versions))
        operations = []
        for desired in core_options_programme_records():
            current = self._get({"PK": desired["PK"], "SK": desired["SK"]})
            if current is not None:
                if _from_storage(current) != desired:
                    raise ProgrammeSeedConflict("Core programme publication conflicts with an existing record")
                continue
            operations.append({"operation": "Put", "TableName": self.table_name, "Item": desired,
                               "ConditionExpression": "attribute_not_exists(PK)"})
        created = len(operations)
        if expected_version != CORE_OPTIONS_VERSION_ID:
            for current in pointers:
                desired = _from_storage(current)
                desired.update(active_version_id=CORE_OPTIONS_VERSION_ID, updated_at=utc_iso(self._now()))
                operations.append({"operation": "Put", "TableName": self.table_name, "Item": desired,
                                   "ConditionExpression": "active_version_id = :version",
                                   "ExpressionAttributeValues": {":version": expected_version}})
        if not dry_run and operations:
            try:
                self._transact_write(operations)
            except Exception as err:
                if _is_conditional_failure(err):
                    raise ProgrammeSeedConflict("Programme changed during publication; reload and retry") from err
                raise
        return {"version_id": CORE_OPTIONS_VERSION_ID, "created": 0 if dry_run else created,
                "would_create": created, "activate": expected_version != CORE_OPTIONS_VERSION_ID,
                "dry_run": dry_run}

    # ---- Identity and profiles -------------------------------------------------

    def resolve_identity(
        self,
        telegram_user_id: int,
        username: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> ServerlessIdentity:
        telegram_user_id = int(telegram_user_id)
        if telegram_user_id <= 0:
            raise ValueError("telegram_user_id must be positive")
        now = utc_iso(self._now())
        key = {"PK": f"IDENTITY#TELEGRAM#{telegram_user_id}", "SK": "USER"}
        existing = self._get(key)
        if existing:
            user_id = str(existing["user_id"])
            created_at = str(existing.get("created_at", now))
        else:
            user_id = str(self.identity_id_factory())
            item = {
                **key,
                "entity_type": "identity",
                "telegram_user_id": telegram_user_id,
                "user_id": user_id,
                "username": str(username or ""),
                "display_name": str(display_name or ""),
                "created_at": now,
                "updated_at": now,
            }
            try:
                self.table.put_item(
                    Item=_to_storage(item),
                    ConditionExpression="attribute_not_exists(PK)",
                )
                return ServerlessIdentity(
                    telegram_user_id,
                    user_id,
                    str(username or ""),
                    str(display_name or ""),
                    now,
                    now,
                    key["PK"],
                )
            except Exception as err:
                if not _is_conditional_failure(err):
                    raise
                existing = self._get(key)
                if not existing:
                    raise IdentityCreationRace("identity was claimed but could not be read") from err
                user_id = str(existing["user_id"])
                created_at = str(existing.get("created_at", now))

        current_username = str(existing.get("username", "") if existing else username or "")
        current_display_name = str(existing.get("display_name", "") if existing else display_name or "")
        new_username = str(username) if username is not None else current_username
        new_display_name = str(display_name) if display_name is not None else current_display_name
        if new_username != current_username or new_display_name != current_display_name:
            self.table.update_item(
                Key=key,
                UpdateExpression="SET username = :username, display_name = :display_name, updated_at = :updated_at",
                ExpressionAttributeValues=_to_storage(
                    {":username": new_username, ":display_name": new_display_name, ":updated_at": now}
                ),
            )
        return ServerlessIdentity(
            telegram_user_id,
            user_id,
            new_username,
            new_display_name,
            created_at,
            now,
            key["PK"],
        )

    def get_identity(self, telegram_user_id: int) -> Optional[ServerlessIdentity]:
        """Read an existing Telegram identity without creating one."""

        telegram_user_id = int(telegram_user_id)
        if telegram_user_id <= 0:
            return None
        item = self._get({"PK": f"IDENTITY#TELEGRAM#{telegram_user_id}", "SK": "USER"})
        if not item:
            return None
        return self._identity_from_item(f"IDENTITY#TELEGRAM#{telegram_user_id}", item)

    def _identity_from_item(self, identity_pk: str, item: Mapping[str, Any]) -> Optional[ServerlessIdentity]:
        if item.get("entity_type") != "identity":
            return None
        try:
            telegram_user_id = int(item.get("telegram_user_id", 0) or 0)
        except (TypeError, ValueError):
            return None
        now = utc_iso(self._now())
        return ServerlessIdentity(
            telegram_user_id=telegram_user_id,
            user_id=str(item.get("user_id", "") or ""),
            username=str(item.get("username", "") or ""),
            display_name=str(item.get("display_name", "") or ""),
            created_at=str(item.get("created_at", now)),
            updated_at=str(item.get("updated_at", item.get("created_at", now))),
            identity_key=identity_pk,
        )

    def get_identity_by_key(self, identity_pk: Any) -> Optional[ServerlessIdentity]:
        """Read a canonical identity by its exact identity partition key."""

        identity_pk = str(identity_pk or "").strip()
        if not identity_pk.startswith("IDENTITY#"):
            return None
        item = self._get({"PK": identity_pk, "SK": "USER"})
        return self._identity_from_item(identity_pk, item) if item else None

    # ---- Browser credentials and sessions ------------------------------------

    def get_web_credential(self, username: Any) -> Optional[dict[str, Any]]:
        """Return a browser credential record without exposing its lookup key."""

        normalized = normalize_web_username(username)
        item = self._get({"PK": web_credential_key(normalized), "SK": "META"})
        if not item:
            return None
        payload = _from_storage(item)
        if payload.get("entity_type") != "web_credential":
            return None
        payload["username"] = normalized
        return payload

    def save_web_credential(
        self,
        username: Any,
        *,
        identity: ServerlessIdentity,
        password_record: Mapping[str, Any],
        replace: bool = False,
    ) -> dict[str, Any]:
        """Persist only hashed credential material for an existing identity."""

        normalized = normalize_web_username(username)
        item = {
            "PK": web_credential_key(normalized),
            "SK": "META",
            "entity_type": "web_credential",
            "username": normalized,
            "user_id": identity.user_id,
            "telegram_user_id": identity.telegram_user_id,
            "identity_pk": identity.identity_pk,
            "created_at": utc_iso(self._now()),
            "updated_at": utc_iso(self._now()),
            **dict(password_record),
        }
        try:
            self.table.put_item(
                Item=_to_storage(item),
                **({} if replace else {"ConditionExpression": "attribute_not_exists(PK)"}),
            )
        except Exception as err:
            if not replace and _is_conditional_failure(err):
                raise WebCredentialExists("browser username already exists") from err
            raise
        return item

    def create_browser_session(
        self,
        identity: ServerlessIdentity,
        *,
        ttl_seconds: int = BROWSER_SESSION_TTL_SECONDS,
    ) -> tuple[str, dict[str, Any]]:
        """Create an opaque, server-side browser session with DynamoDB TTL."""

        now = self._now()
        for _attempt in range(3):
            token = str(self.session_token_factory() or "")
            if not token:
                continue
            item = {
                "PK": f"WEB_SESSION#{browser_session_token_hash(token)}",
                "SK": "META",
                "entity_type": "browser_session",
                "user_id": identity.user_id,
                "telegram_user_id": identity.telegram_user_id,
                "identity_pk": identity.identity_pk,
                "created_at": utc_iso(now),
                "expires_at": epoch_seconds(now) + int(ttl_seconds),
                "active": True,
            }
            try:
                self.table.put_item(Item=_to_storage(item), ConditionExpression="attribute_not_exists(PK)")
                return token, item
            except Exception as err:
                if not _is_conditional_failure(err):
                    raise
        raise DataError("could not allocate browser session")

    def get_browser_session(self, token: Any) -> Optional[dict[str, Any]]:
        """Return a non-expired, non-revoked browser session."""

        token = str(token or "")
        if not token or len(token) > 512:
            return None
        item = self._get({"PK": f"WEB_SESSION#{browser_session_token_hash(token)}", "SK": "META"})
        if not item:
            return None
        payload = _from_storage(item)
        if payload.get("entity_type") != "browser_session" or payload.get("active", True) is False:
            return None
        if int(payload.get("expires_at", 0) or 0) <= epoch_seconds(self._now()):
            return None
        return payload

    def revoke_browser_session(self, token: Any) -> None:
        """Revoke a browser session by deleting its server-side record."""

        token = str(token or "")
        if not token or len(token) > 512:
            return
        self.table.delete_item(Key={"PK": f"WEB_SESSION#{browser_session_token_hash(token)}", "SK": "META"})

    def get_profile(self, user_id: str) -> Optional[UserProfile]:
        item = self._get({"PK": f"USER#{user_id}", "SK": "PROFILE"})
        if not item:
            return None
        payload = _from_storage(item)
        payload["telegram_user_id"] = int(payload.get("telegram_user_id", 0))
        payload["daily_target"] = dict(payload.get("daily_target", {}))
        return UserProfile.from_payload(payload)

    def create_mini_app_launch(
        self,
        token: str,
        *,
        identity: ServerlessIdentity,
        chat_id: int,
        chat_type: str,
        message_id: int,
        launch_type: str = "nutrition",
        requested_day: Optional[str] = None,
        ttl_seconds: int = MINI_APP_LAUNCH_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Persist a short-lived, user-bound Mini App launch context."""

        token = str(token or "").strip()
        if not token:
            raise ValueError("Mini App launch token is required")
        now = self._now()
        item = {
            "PK": f"MINIAPP_LAUNCH#{token}",
            "SK": "META",
            "entity_type": "miniapp_launch",
            "launch_token": token,
            "telegram_user_id": identity.telegram_user_id,
            "user_id": identity.user_id,
            "chat_id": int(chat_id),
            "chat_type": str(chat_type or ""),
            "message_id": int(message_id),
            "launch_type": str(launch_type or "nutrition"),
            "created_at": utc_iso(now),
            "expires_at": epoch_seconds(now) + int(ttl_seconds),
            "active": True,
        }
        if requested_day:
            item["requested_day"] = str(requested_day).strip().upper()
        self.table.put_item(
            Item=_to_storage(item),
            ConditionExpression="attribute_not_exists(PK)",
        )
        return item

    def get_mini_app_launch(self, token: str) -> Optional[dict[str, Any]]:
        """Return an active launch context, excluding expired records."""

        token = str(token or "").strip()
        if not token:
            return None
        item = self._get({"PK": f"MINIAPP_LAUNCH#{token}", "SK": "META"})
        if not item:
            return None
        payload = _from_storage(item)
        if int(payload.get("expires_at", 0) or 0) <= epoch_seconds(self._now()):
            return None
        if payload.get("active", True) is False:
            return None
        return payload

    def save_profile(
        self,
        identity: ServerlessIdentity,
        profile: UserProfile,
        *,
        effective_at: Optional[datetime] = None,
        source: str = "miniapp",
        append_target: bool = True,
    ) -> dict[str, Any]:
        now = utc_iso(self._now())
        created_at = profile.created_at or now
        target_id = uuid.uuid4().hex
        effective_iso = utc_iso(effective_at or self._now())
        target_item = {
            "PK": identity.pk,
            "SK": f"TARGET#{effective_iso}#{target_id}",
            "entity_type": "target",
            "target_id": target_id,
            "effective_at": effective_iso,
            "telegram_user_id": identity.telegram_user_id,
            "user_id": identity.user_id,
            "target": _target_payload(profile.daily_target),
            "questionnaire_answers": profile.questionnaire_answers.to_payload() if profile.questionnaire_answers else None,
            "questionnaire_version": profile.questionnaire_version,
            "source": source,
            "created_at": now,
        }
        profile_item = {
            "PK": identity.pk,
            "SK": "PROFILE",
            "entity_type": "profile",
            "user_id": identity.user_id,
            "telegram_user_id": identity.telegram_user_id,
            "username": profile.username,
            "display_name": profile.display_name,
            "timezone": profile.timezone or DEFAULT_TIMEZONE,
            "daily_target": _target_payload(profile.daily_target),
            "questionnaire_answers": profile.questionnaire_answers.to_payload() if profile.questionnaire_answers else None,
            "questionnaire_version": profile.questionnaire_version,
            **profile.dietary_profile_payload(),
            "created_at": created_at,
            "updated_at": now,
        }
        operations = [{"operation": "Put", "TableName": self.table_name, "Item": profile_item}]
        if append_target:
            operations.append({"operation": "Put", "TableName": self.table_name, "Item": target_item,
                               "ConditionExpression": "attribute_not_exists(PK)"})
        self._transact_write(operations)
        return {"target_id": target_id, "effective_at": effective_iso, "profile": profile}

    def list_targets(self, user_id: str) -> list[dict[str, Any]]:
        items = self._query(
            Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("TARGET#"),
            ScanIndexForward=False,
        )
        return [_from_storage(item) for item in items]

    def target_revision_at(self, user_id: str, at: Optional[datetime] = None) -> Optional[dict[str, Any]]:
        """Return the target revision effective at a UTC instant.

        A profile without target history is an older migrated record, so its
        current target remains the fallback just as it does for
        ``target_effective_at``.
        """

        moment = at or self._now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        cutoff = moment.astimezone(timezone.utc)
        targets = self.list_targets(user_id)
        for item in targets:
            try:
                if parse_utc(str(item.get("effective_at"))) <= cutoff:
                    return {
                        "target_id": str(item.get("target_id", "")),
                        "effective_at": str(item.get("effective_at", "")),
                        "target": dict(item["target"]),
                    }
            except (KeyError, ValueError, TypeError):
                continue
        if targets:
            return None
        profile = self.get_profile(user_id)
        if profile is None:
            return None
        return {"target_id": "", "effective_at": None, "target": profile.daily_target.to_payload()}

    def target_effective_at(self, user_id: str, at: Optional[datetime] = None) -> Optional[MacroTotal]:
        revision = self.target_revision_at(user_id, at=at)
        return MacroTotal.from_payload(revision["target"]) if revision else None

    # ---- Durable workflow ------------------------------------------------------

    def get_workflow(self, user_id: str) -> Optional[dict[str, Any]]:
        item = self._get({"PK": f"USER#{user_id}", "SK": "WORKFLOW#MEAL"})
        if not item:
            return None
        if int(item.get("expires_at", 0)) <= epoch_seconds(self._now()):
            return None
        return _from_storage(item)

    def mark_awaiting_datetime(self, identity: ServerlessIdentity, ttl_seconds: int = WORKFLOW_TTL_SECONDS) -> dict[str, Any]:
        now = self._now()
        item = {
            "PK": identity.pk,
            "SK": "WORKFLOW#MEAL",
            "entity_type": "meal_workflow",
            "state": "awaiting_datetime",
            "telegram_user_id": identity.telegram_user_id,
            "created_at": utc_iso(now),
            "updated_at": utc_iso(now),
            "expires_at": epoch_seconds(now) + int(ttl_seconds),
        }
        self.table.put_item(Item=_to_storage(item))
        return item

    def set_pending_datetime(self, identity: ServerlessIdentity, datetime_iso: str, ttl_seconds: int = WORKFLOW_TTL_SECONDS) -> bool:
        parsed = parse_utc(datetime_iso) if datetime_iso.endswith("Z") or "+" in datetime_iso else datetime.fromisoformat(datetime_iso).replace(tzinfo=timezone.utc)
        normalized = utc_iso(parsed)
        now = self._now()
        try:
            self.table.update_item(
                Key={"PK": identity.pk, "SK": "WORKFLOW#MEAL"},
                UpdateExpression="SET #state = :state, pending_datetime = :pending, updated_at = :updated, expires_at = :expires",
                ConditionExpression="#state = :awaiting AND expires_at > :now",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues=_to_storage(
                    {
                        ":state": "datetime_selected",
                        ":pending": normalized,
                        ":updated": utc_iso(now),
                        ":expires": epoch_seconds(now) + int(ttl_seconds),
                        ":awaiting": "awaiting_datetime",
                        ":now": epoch_seconds(now),
                    }
                ),
            )
            return True
        except Exception as err:
            if _is_conditional_failure(err):
                return False
            raise

    def consume_pending_datetime(self, identity: ServerlessIdentity) -> Optional[str]:
        now = self._now()
        try:
            result = self.table.update_item(
                Key={"PK": identity.pk, "SK": "WORKFLOW#MEAL"},
                UpdateExpression="REMOVE pending_datetime SET #state = :idle, updated_at = :updated",
                ConditionExpression="#state = :selected AND expires_at > :now",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues=_to_storage(
                    {
                        ":selected": "datetime_selected",
                        ":idle": "idle",
                        ":updated": utc_iso(now),
                        ":now": epoch_seconds(now),
                    }
                ),
                ReturnValues="ALL_OLD",
            )
        except Exception as err:
            if _is_conditional_failure(err):
                return None
            raise
        old = result.get("Attributes", {}) if isinstance(result, Mapping) else {}
        return str(old.get("pending_datetime")) if old.get("pending_datetime") else None

    # ---- Meals and details -----------------------------------------------------

    def create_pending_meal(
        self,
        identity: ServerlessIdentity,
        *,
        chat_id: int,
        request_message_id: int,
        caption: str,
        estimate: MealEstimate,
        eaten_at: Optional[datetime] = None,
        username: str = "",
        action_ttl_seconds: int = ACTION_TTL_SECONDS,
        update_id: Optional[int] = None,
        model_metadata: Optional[Mapping[str, Any]] = None,
        telegram_file_id: Optional[str] = None,
        telegram_file_unique_id: Optional[str] = None,
        telegram_message_id: Optional[int] = None,
    ) -> PendingMealAction:
        now = self._now()
        eaten_iso = utc_iso(eaten_at or now)
        meal_id = uuid.uuid4().hex
        token = str(self.token_factory())
        canonical_sk = f"MEAL#{eaten_iso}#{meal_id}"
        detail_prefix = f"MEAL_DETAIL#{eaten_iso}#{meal_id}"
        action_sk = f"ACTION#{token}"
        action_expires_at = epoch_seconds(now) + int(action_ttl_seconds)
        original_payload = _estimate_payload(estimate)
        traceability = {}
        if telegram_file_id:
            traceability["telegram_file_id"] = str(telegram_file_id)
        if telegram_file_unique_id:
            traceability["telegram_file_unique_id"] = str(telegram_file_unique_id)
        if telegram_message_id is not None:
            traceability["telegram_message_id"] = int(telegram_message_id)
        meal_item = {
            "PK": identity.pk,
            "SK": canonical_sk,
            "entity_type": "meal",
            "meal_id": meal_id,
            "user_id": identity.user_id,
            "telegram_user_id": identity.telegram_user_id,
            "eaten_at": eaten_iso,
            "caption": str(caption or "")[:1000],
            "username": str(username or ""),
            "datetime_iso": eaten_iso,
            "status": "pending",
            "original_estimate": original_payload,
            "adjustment_factor": 1.0,
            "chat_id": int(chat_id),
            "request_message_id": int(request_message_id),
            "created_at": utc_iso(now),
            "updated_at": utc_iso(now),
            **traceability,
        }
        pointer_item = {
            "PK": identity.pk,
            "SK": f"MEAL_ID#{meal_id}",
            "entity_type": "meal_pointer",
            "meal_id": meal_id,
            "canonical_sk": canonical_sk,
            "created_at": utc_iso(now),
        }
        detail_item = {
            "PK": identity.pk,
            "SK": f"{detail_prefix}#ESTIMATE",
            "entity_type": "meal_estimate_detail",
            "meal_id": meal_id,
            "canonical_sk": canonical_sk,
            "estimate": original_payload,
            "model_metadata": dict(model_metadata or {}),
            "estimator_version": estimate.estimator_version,
            "created_at": utc_iso(now),
        }
        item_operations: list[dict[str, Any]] = [
            {"operation": "Put", "TableName": self.table_name, "Item": meal_item, "ConditionExpression": "attribute_not_exists(PK)"},
            {"operation": "Put", "TableName": self.table_name, "Item": pointer_item, "ConditionExpression": "attribute_not_exists(PK)"},
            {"operation": "Put", "TableName": self.table_name, "Item": detail_item, "ConditionExpression": "attribute_not_exists(PK)"},
        ]
        for index, item in enumerate(estimate.items, start=1):
            item_operations.append(
                {
                    "operation": "Put",
                    "TableName": self.table_name,
                    "Item": {
                        "PK": identity.pk,
                        "SK": f"{detail_prefix}#ITEM#{index:03d}",
                        "entity_type": "meal_item_detail",
                        "meal_id": meal_id,
                        "item_index": index,
                        "item": {
                            "name": item.name,
                            "portion_g": item.portion_g,
                            "assumptions": item.assumptions,
                            "assumption_categories": dict(item.assumption_categories),
                            "calories": item.calories,
                            "protein_g": item.protein_g,
                            "carbs_g": item.carbs_g,
                            "fat_g": item.fat_g,
                        },
                    },
                }
            )
        action_item = {
            "PK": identity.pk,
            "SK": action_sk,
            "entity_type": "meal_action",
            "token": token,
            "user_id": identity.user_id,
            "telegram_user_id": identity.telegram_user_id,
            "meal_id": meal_id,
            "canonical_sk": canonical_sk,
            "chat_id": int(chat_id),
            "request_message_id": int(request_message_id),
            "message_id": None,
            "caption": str(caption or "")[:1000],
            "datetime_iso": eaten_iso,
            "username": str(username or ""),
            "status": "pending",
            "original_estimate": original_payload,
            "estimate": original_payload,
            "adjustment_factor": 1.0,
            "created_at": utc_iso(now),
            "updated_at": utc_iso(now),
            # `expires_at` is the DynamoDB TTL and deliberately outlives the
            # action deadline so the scheduled sweep can finalize the meal.
            "expires_at": action_expires_at + ACTION_RECORD_RETENTION_SECONDS,
            "action_expires_at": action_expires_at,
            "update_id": update_id,
            "model_metadata": dict(model_metadata or {}),
            "estimator_version": estimate.estimator_version,
            **traceability,
        }
        item_operations.append(
            {
                "operation": "Put",
                "TableName": self.table_name,
                "Item": action_item,
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        )
        self._transact_write(item_operations)
        return _action_from_item(action_item)

    def set_action_message_id(self, identity: ServerlessIdentity, token: str, message_id: int) -> None:
        self.table.update_item(
            Key={"PK": identity.pk, "SK": f"ACTION#{token}"},
            UpdateExpression="SET message_id = :message_id, updated_at = :updated",
            ConditionExpression="#status = :pending",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=_to_storage(
                {":message_id": int(message_id), ":updated": utc_iso(self._now()), ":pending": "pending"}
            ),
        )

    def set_correction_state(self, identity: ServerlessIdentity, token: str, state: str) -> PendingMealAction:
        """Persist the current small correction menu for retry-safe callbacks."""

        action = self.get_action(identity, token)
        if action is None:
            raise ActionNotFound("Meal action was not found")
        now = self._now()
        try:
            self.table.update_item(
                Key={"PK": identity.pk, "SK": f"ACTION#{token}"},
                UpdateExpression="SET correction_state = :state, updated_at = :updated",
                ConditionExpression=_pending_action_live_condition(),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=_to_storage({
                    ":state": str(state or ""),
                    ":updated": utc_iso(now),
                    ":pending": "pending",
                    ":now": epoch_seconds(now),
                }),
            )
        except Exception as err:
            if _is_conditional_failure(err):
                raise ActionFinalized("Meal action is no longer pending") from err
            raise
        return self.get_action(identity, token) or action

    def apply_correction(
        self,
        identity: ServerlessIdentity,
        token: str,
        correction_type: str,
        correction_value: str,
    ) -> PendingMealAction:
        """Apply a deterministic correction and append immutable feedback."""

        action = self.get_action(identity, token)
        if action is None:
            raise ActionNotFound("Meal action was not found")
        now = self._now()
        if int(getattr(action, "expires_at", 0) or 0) <= epoch_seconds(now):
            raise ActionExpired("Meal action expired")
        corrected = action.estimate.adjust_category(correction_type, correction_value)
        if corrected == action.estimate:
            raise ValueError("That correction does not apply to this estimate")
        correction_id = uuid.uuid4().hex
        normalized_type = str(correction_type).strip().lower()
        normalized_value = str(correction_value).strip().lower()
        correction_factor = {
            ("portion", "smaller"): 0.8,
            ("portion", "larger"): 1.2,
        }.get((normalized_type, normalized_value), 1.0)
        correction_item = {
            "PK": identity.pk,
            "SK": f"CORRECTION#{utc_iso(now)}#{correction_id}",
            "entity_type": "meal_correction",
            "correction_id": correction_id,
            "meal_id": action.meal_id,
            "action_token": token,
            "telegram_user_id": identity.telegram_user_id,
            "correction_type": normalized_type,
            "correction_value": normalized_value,
            "original_estimate": _estimate_payload(action.original_estimate or action.estimate),
            "resulting_estimate": _estimate_payload(corrected),
            "original_adjustment_factor": action.adjustment_factor,
            "resulting_adjustment_factor": action.adjustment_factor * correction_factor,
            "created_at": utc_iso(now),
            "final_status": "pending",
            "estimator_version": (action.original_estimate or action.estimate).estimator_version,
        }
        updated_action = {
            "operation": "Update",
            "TableName": self.table_name,
            "Key": {"PK": identity.pk, "SK": f"ACTION#{token}"},
            "UpdateExpression": "SET estimate = :estimate, adjustment_factor = :factor, correction_state = :state, updated_at = :updated",
            "ConditionExpression": _pending_action_live_condition(),
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {
                ":estimate": _estimate_payload(corrected),
                ":factor": action.adjustment_factor * correction_factor,
                ":state": "",
                ":updated": utc_iso(now),
                ":pending": "pending",
                ":now": epoch_seconds(now),
            },
        }
        try:
            self._transact_write([
                updated_action,
                {"operation": "Put", "TableName": self.table_name, "Item": correction_item, "ConditionExpression": "attribute_not_exists(PK)"},
            ])
        except Exception as err:
            if _is_conditional_failure(err):
                raise ActionFinalized("Meal action is no longer pending") from err
            raise
        return self.get_action(identity, token) or action

    def list_corrections(self, identity: ServerlessIdentity, limit: int = 100) -> list[dict[str, Any]]:
        items = self._query(
            Key("PK").eq(identity.pk) & Key("SK").begins_with("CORRECTION#"),
            ScanIndexForward=False,
            Limit=max(1, int(limit)),
        )
        return [_from_storage(item) for item in items]

    def finalize_correction_feedback(self, identity: ServerlessIdentity, token: str, status: str) -> None:
        """Annotate correction records after the action's atomic finalization."""

        for item in self.list_corrections(identity, limit=100):
            if item.get("action_token") != token or item.get("final_status") != "pending":
                continue
            try:
                self.table.update_item(
                    Key={"PK": identity.pk, "SK": item["SK"]},
                    UpdateExpression="SET final_status = :status, finalized_at = :at",
                    ExpressionAttributeValues=_to_storage({":status": str(status), ":at": utc_iso(self._now())}),
                )
            except Exception:
                logger.warning("correction_feedback_finalize_failed", exc_info=True)

    def get_action(self, identity: ServerlessIdentity, token: str) -> Optional[PendingMealAction]:
        item = self._get({"PK": identity.pk, "SK": f"ACTION#{token}"})
        if not item:
            return None
        return _action_from_item(item)

    def find_action_for_update(self, identity: ServerlessIdentity, update_id: int) -> Optional[PendingMealAction]:
        """Find an in-flight action for a retried Telegram update via Query."""

        items = self._query(
            Key("PK").eq(identity.pk) & Key("SK").begins_with("ACTION#"),
            ScanIndexForward=False,
        )
        for item in items:
            if item.get("update_id") is not None and int(item.get("update_id")) == int(update_id):
                return _action_from_item(item)
        return None

    def scale_action(self, identity: ServerlessIdentity, token: str, factor: float) -> PendingMealAction:
        if factor <= 0:
            raise ValueError("scale factor must be positive")
        if factor in {0.8, 1.2}:
            return self.apply_correction(identity, token, "portion", "smaller" if factor < 1 else "larger")
        action = self.get_action(identity, token)
        if action is None:
            raise ActionNotFound("Meal action was not found")
        now = self._now()
        if int(getattr(action, "expires_at", 0) or 0) <= epoch_seconds(now):
            raise ActionExpired("Meal action expired")
        new_estimate = action.estimate.scaled(factor)
        new_factor = action.adjustment_factor * factor
        try:
            self.table.update_item(
                Key={"PK": identity.pk, "SK": f"ACTION#{token}"},
                UpdateExpression="SET estimate = :estimate, adjustment_factor = :factor, updated_at = :updated",
                ConditionExpression=_pending_action_live_condition(),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=_to_storage(
                    {
                        ":estimate": _estimate_payload(new_estimate),
                        ":factor": new_factor,
                        ":updated": utc_iso(now),
                        ":pending": "pending",
                        ":now": epoch_seconds(now),
                    }
                ),
            )
        except Exception as err:
            if _is_conditional_failure(err):
                latest = self.get_action(identity, token)
                if latest and int(getattr(latest, "expires_at", 0) or 0) <= epoch_seconds(now):
                    raise ActionExpired("Meal action expired") from err
                raise ActionFinalized("Meal action is already finalized") from err
            raise
        return self.get_action(identity, token) or action

    def finalize_action(self, identity: ServerlessIdentity, token: str, operation: str) -> FinalizeResult:
        if operation not in {"confirm", "cancel"}:
            raise ValueError("unsupported meal action")
        action = self.get_action(identity, token)
        if action is None:
            raise ActionNotFound("Meal action was not found")
        current_status = _action_status(action)
        if current_status in {"confirmed", "cancelled"}:
            self.finalize_correction_feedback(identity, token, current_status)
            return FinalizeResult(current_status, action, self.get_meal(identity, action.meal_id), duplicate=True)
        now = self._now()
        new_status = "confirmed" if operation == "confirm" else "cancelled"
        final_payload = _estimate_payload(action.estimate) if operation == "confirm" else None
        action_update = {
            "operation": "Update",
            "TableName": self.table_name,
            "Key": {"PK": identity.pk, "SK": f"ACTION#{token}"},
            "UpdateExpression": "SET #status = :new_status, finalized_at = :finalized, updated_at = :updated",
            "ConditionExpression": _pending_action_live_condition(),
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {
                ":new_status": new_status,
                ":finalized": utc_iso(now),
                ":updated": utc_iso(now),
                ":pending": "pending",
                ":now": epoch_seconds(now),
            },
        }
        meal_values = {
            ":new_status": new_status,
            ":updated": utc_iso(now),
            ":pending": "pending",
            ":factor": action.adjustment_factor,
        }
        meal_expression = "SET #status = :new_status, adjustment_factor = :factor, updated_at = :updated"
        if final_payload is not None:
            meal_expression += ", final_estimate = :final_estimate, final_macros = :final_macros, confirmed_at = :updated"
            meal_values[":final_estimate"] = final_payload
            meal_values[":final_macros"] = action.estimate.total_best.to_payload()
        meal_update = {
            "operation": "Update",
            "TableName": self.table_name,
            "Key": {"PK": identity.pk, "SK": action.canonical_sk},
            "UpdateExpression": meal_expression,
            "ConditionExpression": "#status = :pending",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": meal_values,
        }
        try:
            self._transact_write([action_update, meal_update])
        except Exception as err:
            if not _is_conditional_failure(err):
                raise
            latest = self.get_action(identity, token)
            if latest and _action_status(latest) in {"confirmed", "cancelled"}:
                return FinalizeResult(_action_status(latest), latest, self.get_meal(identity, latest.meal_id), duplicate=True)
            if latest and int(getattr(latest, "expires_at", 0) or 0) <= epoch_seconds(now):
                raise ActionExpired("Meal action expired") from err
            raise ActionFinalized("Meal action is no longer pending") from err
        finalized = self.get_action(identity, token) or action
        self.finalize_correction_feedback(identity, token, new_status)
        return FinalizeResult(new_status, finalized, self.get_meal(identity, action.meal_id), duplicate=False)

    def auto_confirm_expired_action(self, identity: ServerlessIdentity, token: str) -> FinalizeResult:
        """Confirm an expired pending action exactly once."""

        action = self.get_action(identity, token)
        if action is None:
            raise ActionNotFound("Meal action was not found")
        current_status = _action_status(action)
        if current_status in {"confirmed", "cancelled"}:
            return FinalizeResult(current_status, action, self.get_meal(identity, action.meal_id), duplicate=True)
        now = self._now()
        if int(getattr(action, "expires_at", 0) or 0) > epoch_seconds(now):
            raise ActionFinalized("Meal action is still active")

        final_payload = _estimate_payload(action.estimate)
        action_update = {
            "operation": "Update",
            "TableName": self.table_name,
            "Key": {"PK": identity.pk, "SK": f"ACTION#{token}"},
            "UpdateExpression": (
                "SET #status = :confirmed, finalized_at = :finalized, "
                "finalization_reason = :reason, updated_at = :updated"
            ),
            "ConditionExpression": _pending_action_expired_condition(),
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {
                ":confirmed": "confirmed",
                ":finalized": utc_iso(now),
                ":reason": "expired_auto_confirm",
                ":updated": utc_iso(now),
                ":pending": "pending",
                ":now": epoch_seconds(now),
            },
        }
        meal_update = {
            "operation": "Update",
            "TableName": self.table_name,
            "Key": {"PK": identity.pk, "SK": action.canonical_sk},
            "UpdateExpression": (
                "SET #status = :confirmed, adjustment_factor = :factor, "
                "final_estimate = :final_estimate, final_macros = :final_macros, "
                "updated_at = :updated, confirmed_at = :updated"
            ),
            "ConditionExpression": "#status = :pending",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {
                ":confirmed": "confirmed",
                ":factor": action.adjustment_factor,
                ":final_estimate": final_payload,
                ":final_macros": action.estimate.total_best.to_payload(),
                ":updated": utc_iso(now),
                ":pending": "pending",
            },
        }
        try:
            self._transact_write([action_update, meal_update])
        except Exception as err:
            if not _is_conditional_failure(err):
                raise
            latest = self.get_action(identity, token)
            if latest and _action_status(latest) in {"confirmed", "cancelled"}:
                return FinalizeResult(_action_status(latest), latest, self.get_meal(identity, latest.meal_id), duplicate=True)
            raise ActionFinalized("Meal action was finalized concurrently") from err
        finalized = self.get_action(identity, token) or action
        self.finalize_correction_feedback(identity, token, "confirmed")
        return FinalizeResult("confirmed", finalized, self.get_meal(identity, action.meal_id), duplicate=False)

    def expire_pending_actions(self, *, limit: int = 100) -> list[PendingMealAction]:
        """Auto-confirm expired pending actions and return their message data."""

        now = epoch_seconds(self._now())
        filter_expression = (
            Attr("entity_type").eq("meal_action")
            & Attr("status").eq("pending")
            & (
                Attr("action_expires_at").lte(now)
                | (Attr("action_expires_at").not_exists() & Attr("expires_at").lte(now))
            )
        )
        scan_kwargs: dict[str, Any] = {"FilterExpression": filter_expression}
        expired: list[PendingMealAction] = []
        while len(expired) < int(limit):
            response = self.table.scan(**scan_kwargs)
            for raw_item in response.get("Items", []) if isinstance(response, Mapping) else []:
                item = dict(raw_item)
                action = _action_from_item(item)
                user_id = str(item.get("PK", "")).removeprefix("USER#")
                if not user_id:
                    continue
                identity = ServerlessIdentity(
                    action.telegram_user_id,
                    user_id,
                    action.username or "",
                    "",
                    "",
                    "",
                )
                try:
                    result = self.auto_confirm_expired_action(identity, action.token)
                except (ActionNotFound, ActionFinalized):
                    continue
                if result.status == "confirmed" and not result.duplicate:
                    expired.append(result.action)
                    if len(expired) >= int(limit):
                        break
            last_key = response.get("LastEvaluatedKey") if isinstance(response, Mapping) else None
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key
        return expired

    def get_meal(self, identity: ServerlessIdentity, meal_id: str) -> Optional[StoredMeal]:
        pointer = self._get({"PK": identity.pk, "SK": f"MEAL_ID#{meal_id}"})
        if not pointer:
            return None
        canonical_sk = str(pointer.get("canonical_sk", ""))
        item = self._get({"PK": identity.pk, "SK": canonical_sk}) if canonical_sk else None
        return _meal_from_item(item) if item else None

    def list_meals_between(self, identity: ServerlessIdentity, start_iso: str, end_iso: str, *, confirmed_only: bool = True) -> list[StoredMeal]:
        items = self._query(
            Key("PK").eq(identity.pk) & Key("SK").between(f"MEAL#{start_iso}", f"MEAL#{end_iso}"),
            ScanIndexForward=True,
            ConsistentRead=True,
        )
        meals = [_meal_from_item(item) for item in items]
        if confirmed_only:
            meals = [meal for meal in meals if meal.status == "confirmed"]
        return sorted(meals, key=lambda meal: (parse_utc(meal.eaten_at), meal.meal_id))

    def list_recent_meals(self, identity: ServerlessIdentity, limit: int = 6) -> list[StoredMeal]:
        items = self._query(
            Key("PK").eq(identity.pk) & Key("SK").begins_with("MEAL#"),
            ScanIndexForward=False,
            Limit=max(1, int(limit)),
            FilterExpression=Attr("status").eq("confirmed"),
        )
        meals = [_meal_from_item(item) for item in items]
        return sorted(meals, key=lambda meal: (parse_utc(meal.eaten_at), meal.meal_id))

    def daily_summary(self, identity: ServerlessIdentity, target_date: date, timezone_name: str = DEFAULT_TIMEZONE) -> DailyMacroSummary:
        start_iso, end_iso = local_day_utc_bounds(target_date, timezone_name)
        meals = self.list_meals_between(identity, start_iso, end_iso, confirmed_only=True)
        totals = MacroTotal(
            calories=sum(meal.macros.calories for meal in meals),
            protein_g=sum(meal.macros.protein_g for meal in meals),
            carbs_g=sum(meal.macros.carbs_g for meal in meals),
            fat_g=sum(meal.macros.fat_g for meal in meals),
        )
        rows = [_logged_row(meal) for meal in meals]
        return DailyMacroSummary(identity.telegram_user_id, target_date.isoformat(), totals, rows)

    def persona_hint(self, identity: ServerlessIdentity, caption: str) -> str:
        """Return a bounded, confirmed-correction context for this user only."""

        del caption  # Current caption is stronger evidence and is supplied separately.
        counts: dict[tuple[str, str], int] = {}
        for correction in self.list_corrections(identity, limit=100):
            if correction.get("final_status") != "confirmed":
                continue
            key = (str(correction.get("correction_type", "")), str(correction.get("correction_value", "")))
            if key[0] and key[1]:
                counts[key] = counts.get(key, 0) + 1
        priors = []
        labels = {
            ("base", "half"): "similar meals often have half the usual base portion",
            ("skin", "removed"): "chicken skin is usually removed",
            ("sauce", "light"): "sauce/oil is usually light",
            ("sauce", "heavy"): "sauce/oil is usually heavy",
        }
        for key, count in sorted(counts.items(), key=lambda entry: (-entry[1], entry[0])):
            if count < 2:
                continue
            label = labels.get(key, f"repeated correction: {key[0]}={key[1]}")
            priors.append(f"{label} ({count} confirmed examples)")
        if not priors:
            return ""
        return (
            "Historical weak priors (current image/caption override these): "
            + "; ".join(priors[:3])
            + ". Use only when the current meal appears similar."
        )


def _action_status(action: PendingMealAction) -> str:
    return str(action.status)


def _pending_action_live_condition() -> str:
    """Condition supporting new action deadlines and legacy action records."""

    return (
        "#status = :pending AND "
        "((attribute_exists(action_expires_at) AND action_expires_at > :now) "
        "OR (attribute_not_exists(action_expires_at) AND expires_at > :now))"
    )


def _pending_action_expired_condition() -> str:
    """Condition used to atomically claim an expired action for auto-logging."""

    return (
        "#status = :pending AND "
        "((attribute_exists(action_expires_at) AND action_expires_at <= :now) "
        "OR (attribute_not_exists(action_expires_at) AND expires_at <= :now))"
    )


def _action_from_item(item: Mapping[str, Any]) -> PendingMealAction:
    plain = _from_storage(item)
    return PendingMealAction(
        token=str(plain["token"]),
        chat_id=int(plain.get("chat_id", 0)),
        request_message_id=int(plain.get("request_message_id", 0)),
        telegram_user_id=int(plain.get("telegram_user_id", 0)),
        username=str(plain.get("username", "") or "") or None,
        caption=str(plain.get("caption", "") or ""),
        estimate=_estimate_from_payload(plain["estimate"]),
        status=str(plain.get("status", "pending")),
        datetime_iso=str(plain.get("datetime_iso", "") or "") or None,
        message_id=int(plain["message_id"]) if plain.get("message_id") is not None else None,
        created_at=parse_utc(str(plain.get("created_at", utc_iso()))),
        adjustment_factor=float(plain.get("adjustment_factor", 1.0)),
        metrics_event_id=plain.get("estimate", {}).get("metrics_event_id"),
        meal_id=str(plain.get("meal_id", "")) or None,
        canonical_sk=str(plain.get("canonical_sk", "")) or None,
        expires_at=int(plain.get("action_expires_at", plain.get("expires_at", 0)) or 0),
        original_estimate=_estimate_from_payload(plain["original_estimate"]) if plain.get("original_estimate") else None,
        correction_state=str(plain.get("correction_state", "") or ""),
    )


def _meal_from_item(item: Mapping[str, Any]) -> StoredMeal:
    plain = _from_storage(item)
    original = _estimate_from_payload(plain["original_estimate"])
    final_payload = plain.get("final_estimate")
    return StoredMeal(
        meal_id=str(plain["meal_id"]),
        user_id=str(plain.get("user_id", "")),
        telegram_user_id=int(plain.get("telegram_user_id", 0)),
        eaten_at=str(plain["eaten_at"]),
        caption=str(plain.get("caption", "") or ""),
        username=str(plain.get("username", "") or ""),
        status=str(plain.get("status", "pending")),
        original_estimate=original,
        final_estimate=_estimate_from_payload(final_payload) if isinstance(final_payload, Mapping) else None,
        adjustment_factor=float(plain.get("adjustment_factor", 1.0)),
        message_id=int(plain["message_id"]) if plain.get("message_id") is not None else None,
        created_at=str(plain.get("created_at", "")),
        updated_at=str(plain.get("updated_at", "")),
        confirmed_at=str(plain["confirmed_at"]) if plain.get("confirmed_at") else None,
    )


def _logged_row(meal: StoredMeal) -> LoggedMealRow:
    macros = meal.macros
    return LoggedMealRow(
        datetime_iso=meal.eaten_at,
        telegram_user_id=meal.telegram_user_id,
        username=meal.username,
        person="runtime",
        caption=meal.caption,
        calories=int(round(macros.calories)),
        protein_g=round(macros.protein_g, 1),
        carbs_g=round(macros.carbs_g, 1),
        fat_g=round(macros.fat_g, 1),
        confidence=round(float((meal.final_estimate or meal.original_estimate).confidence), 3),
        message_id=meal.message_id or 0,
        confirmed_at=meal.confirmed_at,
    )


def _normalize_caption(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())
