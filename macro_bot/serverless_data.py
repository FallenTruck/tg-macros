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

from .models import (
    DailyMacroSummary,
    LoggedMealRow,
    MacroTotal,
    MealEstimate,
    PendingMealAction,
    UserProfile,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Asia/Singapore"
WORKFLOW_TTL_SECONDS = 30 * 60
ACTION_TTL_SECONDS = 60 * 60


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


@dataclass(frozen=True)
class ServerlessIdentity:
    telegram_user_id: int
    user_id: str
    username: str
    display_name: str
    created_at: str
    updated_at: str

    @property
    def pk(self) -> str:
        return f"USER#{self.user_id}"

    @property
    def identity_pk(self) -> str:
        return f"IDENTITY#TELEGRAM#{self.telegram_user_id}"


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

    @property
    def macros(self) -> MacroTotal:
        return (self.final_estimate or self.original_estimate).total_best


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
                return ServerlessIdentity(telegram_user_id, user_id, str(username or ""), str(display_name or ""), now, now)
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
        return ServerlessIdentity(telegram_user_id, user_id, new_username, new_display_name, created_at, now)

    def get_profile(self, user_id: str) -> Optional[UserProfile]:
        item = self._get({"PK": f"USER#{user_id}", "SK": "PROFILE"})
        if not item:
            return None
        payload = _from_storage(item)
        payload["telegram_user_id"] = int(payload.get("telegram_user_id", 0))
        payload["daily_target"] = dict(payload.get("daily_target", {}))
        return UserProfile.from_payload(payload)

    def save_profile(
        self,
        identity: ServerlessIdentity,
        profile: UserProfile,
        *,
        effective_at: Optional[datetime] = None,
        source: str = "miniapp",
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
            "dietary_preferences": list(profile.dietary_preferences),
            "restrictions": list(profile.restrictions),
            "preferred_cuisines": list(profile.preferred_cuisines),
            "preferred_staples": list(profile.preferred_staples),
            "preferred_tags": list(profile.preferred_tags),
            "created_at": created_at,
            "updated_at": now,
        }
        self._transact_write(
            [
                {"operation": "Put", "TableName": self.table_name, "Item": profile_item},
                {
                    "operation": "Put",
                    "TableName": self.table_name,
                    "Item": target_item,
                    "ConditionExpression": "attribute_not_exists(PK)",
                },
            ]
        )
        return {"target_id": target_id, "effective_at": effective_iso, "profile": profile}

    def list_targets(self, user_id: str) -> list[dict[str, Any]]:
        items = self._query(
            Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("TARGET#"),
            ScanIndexForward=False,
        )
        return [_from_storage(item) for item in items]

    def target_effective_at(self, user_id: str, at: Optional[datetime] = None) -> Optional[MacroTotal]:
        moment = at or self._now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        cutoff = moment.astimezone(timezone.utc)
        targets = self.list_targets(user_id)
        for item in targets:
            try:
                if parse_utc(str(item.get("effective_at"))) <= cutoff:
                    return MacroTotal.from_payload(dict(item["target"]))
            except (KeyError, ValueError, TypeError):
                continue
        # A profile target is the current fallback only when no target history
        # exists at all. A lookup before the first effective target must not
        # incorrectly report a later/current target.
        if targets:
            return None
        profile = self.get_profile(user_id)
        return profile.daily_target if profile else None

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
        expires_at = epoch_seconds(now) + int(action_ttl_seconds)
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
            "expires_at": expires_at,
            "update_id": update_id,
            "model_metadata": dict(model_metadata or {}),
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
                ConditionExpression="#status = :pending AND expires_at > :now",
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
            return FinalizeResult(current_status, action, self.get_meal(identity, action.meal_id), duplicate=True)
        now = self._now()
        new_status = "confirmed" if operation == "confirm" else "cancelled"
        final_payload = _estimate_payload(action.estimate) if operation == "confirm" else None
        action_update = {
            "operation": "Update",
            "TableName": self.table_name,
            "Key": {"PK": identity.pk, "SK": f"ACTION#{token}"},
            "UpdateExpression": "SET #status = :new_status, finalized_at = :finalized, updated_at = :updated",
            "ConditionExpression": "#status = :pending AND expires_at > :now",
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
            meal_expression += ", final_estimate = :final_estimate, final_macros = :final_macros"
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
        return FinalizeResult(new_status, finalized, self.get_meal(identity, action.meal_id), duplicate=False)

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
        )
        meals = [_meal_from_item(item) for item in items]
        if confirmed_only:
            meals = [meal for meal in meals if meal.status == "confirmed"]
        return meals

    def list_recent_meals(self, identity: ServerlessIdentity, limit: int = 6) -> list[StoredMeal]:
        items = self._query(
            Key("PK").eq(identity.pk) & Key("SK").begins_with("MEAL#"),
            ScanIndexForward=False,
            Limit=max(1, int(limit)),
            FilterExpression=Attr("status").eq("confirmed"),
        )
        meals = [_meal_from_item(item) for item in items]
        return list(reversed(meals))

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
        normalized = _normalize_caption(caption)
        if not normalized:
            return ""
        for meal in reversed(self.list_recent_meals(identity, limit=20)):
            if _normalize_caption(meal.caption) == normalized:
                return (
                    "Similar prior meal detected. Prior estimate context: "
                    f"meal={meal.original_estimate.meal_name}, calories={int(round(meal.macros.calories))} kcal. "
                    "Use this as a soft prior only if image appears similar."
                )
        return ""


def _action_status(action: PendingMealAction) -> str:
    return str(action.status)


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
        expires_at=int(plain.get("expires_at", 0) or 0),
        original_estimate=_estimate_from_payload(plain["original_estimate"]) if plain.get("original_estimate") else None,
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
    )


def _normalize_caption(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())
