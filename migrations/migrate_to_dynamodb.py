#!/usr/bin/env python3
"""Migrate the authoritative legacy nutrition files into DynamoDB.

This tool intentionally imports only the canonical profile JSON and meals_v2
CSV.  It does not call Telegram or OpenAI, and it never imports estimator
metrics as meal detail.  Historical meals are stored as confirmed,
meal-level records because the legacy source has no reliable item breakdown.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from boto3.dynamodb.conditions import Key

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from macro_bot.models import LoggedMealRow, UserProfile  # noqa: E402
from macro_bot.serverless_data import (  # noqa: E402
    DynamoNutritionRepository,
    _from_storage,
    _to_storage,
    utc_iso,
)

LOGGER = logging.getLogger("legacy_migration")
MIGRATION_VERSION = "legacy-nutrition-v1"
LEGACY_TIMEZONE = "Asia/Singapore"
DEFAULT_TABLE_NAME = "tg-macros-dev-fitness-data"
DEFAULT_PROFILE_PATH = ROOT / "user_profiles.json"
DEFAULT_MEALS_PATH = ROOT / "meals_v2.csv"
CHECKSUM_PATHS = (
    ROOT / "user_profiles.json",
    ROOT / "meals_v2.csv",
    ROOT / "food_catalog.json",
    ROOT / "catalog_suggestions.json",
    ROOT / "metrics" / "estimates.jsonl",
)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class LegacyProfile:
    source_row: int
    profile: UserProfile
    source_updated_at: str


@dataclass(frozen=True)
class LegacyMeal:
    source_row: int
    source_datetime: str
    eaten_at: datetime
    assumed_timezone: bool
    telegram_user_id: int
    username: str
    person: str
    caption: str
    calories: int
    protein_g: float
    carbs_g: float
    fat_g: float
    confidence: float
    message_id: int

    @property
    def meal_id(self) -> str:
        payload = {
            "source_file": "meals_v2.csv",
            "source_row": self.source_row,
            "source_datetime": self.source_datetime,
            "telegram_user_id": self.telegram_user_id,
            "username": self.username,
            "person": self.person,
            "caption": self.caption,
            "calories": self.calories,
            "protein_g": self.protein_g,
            "carbs_g": self.carbs_g,
            "fat_g": self.fat_g,
            "confidence": self.confidence,
            "message_id": self.message_id,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return f"legacy-{digest[:32]}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_checksums() -> dict[str, str]:
    return {str(path.relative_to(ROOT)): _sha256(path) for path in CHECKSUM_PATHS if path.exists()}


def parse_legacy_datetime(value: str) -> tuple[datetime, bool]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("datetime is empty")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    assumed_timezone = parsed.tzinfo is None
    if assumed_timezone:
        parsed = parsed.replace(tzinfo=ZoneInfo(LEGACY_TIMEZONE))
    return parsed.astimezone(timezone.utc), assumed_timezone


def load_profiles(path: Path) -> list[LegacyProfile]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(raw_profiles, list):
        raise MigrationError("user_profiles.json must contain a profiles list")
    profiles: list[LegacyProfile] = []
    seen: set[int] = set()
    for source_row, raw_profile in enumerate(raw_profiles, start=1):
        profile = UserProfile.from_payload(raw_profile)
        if profile.telegram_user_id in seen:
            raise MigrationError(f"duplicate profile telegram_user_id at source row {source_row}")
        seen.add(profile.telegram_user_id)
        profiles.append(LegacyProfile(source_row, profile, str(raw_profile.get("updated_at", "") or "")))
    return profiles


def load_meals(path: Path) -> tuple[list[LegacyMeal], list[dict[str, Any]]]:
    meals: list[LegacyMeal] = []
    malformed: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "datetime", "telegram_user_id", "username", "person", "caption", "calories",
            "protein_g", "carbs_g", "fat_g", "confidence", "message_id",
        }
        missing_header = sorted(required - set(reader.fieldnames or []))
        if missing_header:
            raise MigrationError(f"meals_v2.csv missing columns: {', '.join(missing_header)}")
        for row in reader:
            source_row = reader.line_num
            try:
                parsed_datetime, assumed_timezone = parse_legacy_datetime(row.get("datetime", ""))
                typed = LoggedMealRow.from_csv_row(row)
                if typed.telegram_user_id <= 0:
                    raise ValueError("telegram_user_id must be positive")
                if typed.message_id <= 0:
                    raise ValueError("message_id must be positive")
                meals.append(
                    LegacyMeal(
                        source_row=source_row,
                        source_datetime=str(row.get("datetime", "")),
                        eaten_at=parsed_datetime,
                        assumed_timezone=assumed_timezone,
                        telegram_user_id=typed.telegram_user_id,
                        username=typed.username,
                        person=typed.person,
                        caption=typed.caption,
                        calories=typed.calories,
                        protein_g=typed.protein_g,
                        carbs_g=typed.carbs_g,
                        fat_g=typed.fat_g,
                        confidence=typed.confidence,
                        message_id=typed.message_id,
                    )
                )
            except (TypeError, ValueError, KeyError) as err:
                malformed.append({"source_file": "meals_v2.csv", "source_row": source_row, "error": type(err).__name__})
    return meals, malformed


def validate_sources(profiles: list[LegacyProfile], meals: list[LegacyMeal], malformed: list[dict[str, Any]]) -> None:
    if malformed:
        raise MigrationError(f"malformed legacy rows: {len(malformed)}")
    profile_ids = {entry.profile.telegram_user_id for entry in profiles}
    meal_counts: dict[int, int] = {telegram_id: 0 for telegram_id in profile_ids}
    for meal in meals:
        if meal.telegram_user_id not in profile_ids:
            raise MigrationError(f"meal source row {meal.source_row} has no matching profile")
        meal_counts[meal.telegram_user_id] += 1
    expected_counts = {349553317: 19, 559404539: 17}
    if len(profiles) != 2 or len(meals) != 36:
        raise MigrationError(f"expected 2 profiles and 36 meals, found {len(profiles)} and {len(meals)}")
    if meal_counts != expected_counts:
        raise MigrationError(f"unexpected per-user meal counts: {meal_counts}")


def _meaningful(value: Any) -> bool:
    return value not in (None, "", [], {}, ())


def _profile_item(identity: Any, entry: LegacyProfile, imported_at: str) -> dict[str, Any]:
    profile = entry.profile
    payload = profile.to_payload()
    return {
        "PK": identity.pk,
        "SK": "PROFILE",
        "entity_type": "profile",
        "user_id": identity.user_id,
        "telegram_user_id": identity.telegram_user_id,
        "username": profile.username,
        "display_name": profile.display_name,
        "timezone": profile.timezone or LEGACY_TIMEZONE,
        "daily_target": profile.daily_target.to_payload(),
        "questionnaire_answers": payload.get("questionnaire_answers"),
        "questionnaire_version": profile.questionnaire_version,
        "dietary_preferences": list(profile.dietary_preferences),
        "restrictions": list(profile.restrictions),
        "preferred_cuisines": list(profile.preferred_cuisines),
        "preferred_staples": list(profile.preferred_staples),
        "preferred_tags": list(profile.preferred_tags),
        "created_at": imported_at,
        "updated_at": imported_at,
        "source": "legacy_import",
        "source_file": "user_profiles.json",
        "source_row": entry.source_row,
        "migration_version": MIGRATION_VERSION,
        "imported_at": imported_at,
        "legacy_updated_at": entry.source_updated_at,
    }


def _target_key(identity: Any, target_id: str, effective_at: str) -> dict[str, str]:
    return {"PK": identity.pk, "SK": f"TARGET#{effective_at}#{target_id}"}


def _target_id(entry: LegacyProfile) -> str:
    payload = {
        "source_file": "user_profiles.json",
        "source_row": entry.source_row,
        "telegram_user_id": entry.profile.telegram_user_id,
        "daily_target": entry.profile.daily_target.to_payload(),
        "source_updated_at": entry.source_updated_at,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"legacy-{digest[:32]}"


def _target_item(identity: Any, entry: LegacyProfile, effective_at: str, imported_at: str) -> dict[str, Any]:
    profile = entry.profile
    target_id = _target_id(entry)
    source_effective = entry.source_updated_at or "migration-assumption: profile updated_at unavailable"
    return {
        **_target_key(identity, target_id, effective_at),
        "entity_type": "target",
        "target_id": target_id,
        "effective_at": effective_at,
        "telegram_user_id": identity.telegram_user_id,
        "user_id": identity.user_id,
        "target": profile.daily_target.to_payload(),
        "questionnaire_answers": profile.questionnaire_answers.to_payload() if profile.questionnaire_answers else None,
        "questionnaire_version": profile.questionnaire_version,
        "source": "legacy_import",
        "source_file": "user_profiles.json",
        "source_row": entry.source_row,
        "migration_version": MIGRATION_VERSION,
        "imported_at": imported_at,
        "legacy_updated_at": source_effective,
        "effective_at_assumption": "profile.updated_at interpreted as Asia/Singapore" if entry.source_updated_at else "fixed migration assumption",
        "created_at": imported_at,
    }


def _historical_estimate(meal: LegacyMeal) -> dict[str, Any]:
    # Deliberately omit items, ranges, variance drivers, and new estimator
    # quality fields: the legacy source contains only confirmed meal totals.
    return {
        "meal_name": meal.caption[:1000] or "Legacy meal",
        "calories": meal.calories,
        "protein_g": meal.protein_g,
        "carbs_g": meal.carbs_g,
        "fat_g": meal.fat_g,
        "confidence": meal.confidence,
        "notes": "Imported legacy confirmed meal; structured detail unavailable.",
    }


def _meal_items(identity: Any, meal: LegacyMeal, imported_at: str) -> tuple[dict[str, Any], dict[str, Any]]:
    eaten_at = utc_iso(meal.eaten_at)
    meal_id = meal.meal_id
    canonical_sk = f"MEAL#{eaten_at}#{meal_id}"
    metadata = {
        "source": "legacy_import",
        "source_file": "meals_v2.csv",
        "source_row": meal.source_row,
        "migration_version": MIGRATION_VERSION,
        "imported_at": imported_at,
        "legacy_datetime": meal.source_datetime,
        "legacy_person": meal.person,
    }
    estimate = _historical_estimate(meal)
    canonical = {
        "PK": identity.pk,
        "SK": canonical_sk,
        "entity_type": "meal",
        "meal_id": meal_id,
        "user_id": identity.user_id,
        "telegram_user_id": identity.telegram_user_id,
        "eaten_at": eaten_at,
        "caption": meal.caption[:1000],
        "username": meal.username,
        "datetime_iso": eaten_at,
        "status": "confirmed",
        "original_estimate": estimate,
        "adjustment_factor": 1.0,
        "message_id": meal.message_id,
        "request_message_id": meal.message_id,
        "created_at": imported_at,
        "updated_at": imported_at,
        **metadata,
    }
    pointer = {
        "PK": identity.pk,
        "SK": f"MEAL_ID#{meal_id}",
        "entity_type": "meal_pointer",
        "meal_id": meal_id,
        "canonical_sk": canonical_sk,
        "user_id": identity.user_id,
        **metadata,
    }
    return canonical, pointer


def _same_number(left: Any, right: Any) -> bool:
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return left == right


def _meal_matches(existing: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    estimate = existing.get("original_estimate") or {}
    expected_estimate = expected.get("original_estimate") or {}
    for key in ("user_id", "telegram_user_id", "meal_id", "eaten_at", "caption", "username", "status", "message_id", "source_file", "source_row"):
        matches = (
            _same_number(existing.get(key), expected.get(key))
            if key in {"telegram_user_id", "message_id", "source_row"}
            else existing.get(key) == expected.get(key)
        )
        if not matches:
            return False
    for key in ("meal_name", "calories", "protein_g", "carbs_g", "fat_g", "confidence"):
        if key not in estimate or not _same_number(estimate.get(key), expected_estimate.get(key)):
            return False
    return True


def _pointer_matches(existing: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(existing.get(key) == expected.get(key) for key in ("entity_type", "meal_id", "canonical_sk", "user_id"))


def _effective_target_timestamp(entry: LegacyProfile) -> tuple[str, str]:
    if entry.source_updated_at:
        parsed, _ = parse_legacy_datetime(entry.source_updated_at)
        return utc_iso(parsed), "profile.updated_at interpreted as Asia/Singapore"
    assumption = datetime(2026, 1, 1, tzinfo=ZoneInfo(LEGACY_TIMEZONE)).astimezone(timezone.utc)
    return utc_iso(assumption), "fixed migration-assumption timestamp; legacy updated_at unavailable"


class MigrationRunner:
    def __init__(self, table: Any, *, client: Any = None, now_fn: Any = None):
        self.table = table
        self.repository = DynamoNutritionRepository(
            table=table,
            table_name=getattr(table, "name", DEFAULT_TABLE_NAME),
            client=client,
            now_fn=now_fn or (lambda: datetime.now(timezone.utc)),
        )

    def _get(self, key: Mapping[str, str]) -> Optional[dict[str, Any]]:
        result = self.table.get_item(Key=dict(key), ConsistentRead=True)
        item = result.get("Item") if isinstance(result, Mapping) else None
        return _from_storage(item) if item else None

    def identity_record(self, telegram_user_id: int) -> Optional[dict[str, Any]]:
        return self._get({"PK": f"IDENTITY#TELEGRAM#{telegram_user_id}", "SK": "USER"})

    def profile_record(self, user_id: str) -> Optional[dict[str, Any]]:
        return self._get({"PK": f"USER#{user_id}", "SK": "PROFILE"})

    def _put_if_absent(self, item: Mapping[str, Any]) -> bool:
        try:
            self.table.put_item(Item=_to_storage(dict(item)), ConditionExpression="attribute_not_exists(PK)")
            return True
        except Exception as err:
            if type(err).__name__ != "ConditionalCheckFailedException":
                raise
            return False

    def _merge_profile(self, identity: Any, entry: LegacyProfile, imported_at: str, *, dry_run: bool) -> tuple[str, list[str]]:
        expected = _profile_item(identity, entry, imported_at)
        existing = self.profile_record(identity.user_id)
        if not existing:
            if not dry_run:
                self._put_if_absent(expected)
            return "create", []

        merge_fields = (
            "username", "display_name", "timezone", "daily_target", "questionnaire_answers",
            "questionnaire_version", "dietary_preferences", "restrictions", "preferred_cuisines",
            "preferred_staples", "preferred_tags",
        )
        updates: dict[str, Any] = {}
        conflicts: list[str] = []
        for field in merge_fields:
            source_value = expected.get(field)
            current_value = existing.get(field)
            if _meaningful(current_value) and _meaningful(source_value) and current_value != source_value:
                conflicts.append(field)
            elif not _meaningful(current_value) and _meaningful(source_value):
                updates[field] = source_value
        if conflicts:
            return "conflict", conflicts
        if updates and not dry_run:
            names = {f"#f{index}": field for index, field in enumerate(updates)}
            values = {f":v{index}": _to_storage(value) for index, value in enumerate(updates.values())}
            assignments = ", ".join(f"{name} = :v{index}" for index, name in enumerate(names.values()))
            self.table.update_item(
                Key={"PK": identity.pk, "SK": "PROFILE"},
                UpdateExpression=f"SET {assignments}",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(PK)",
            )
        return ("merge" if updates else "preserve"), []

    def _target_state(self, identity: Any, entry: LegacyProfile, imported_at: str, *, dry_run: bool) -> tuple[str, Optional[str]]:
        effective_at, _ = _effective_target_timestamp(entry)
        expected = _target_item(identity, entry, effective_at, imported_at)
        candidates = self.table.query(
            KeyConditionExpression=Key("PK").eq(identity.pk) & Key("SK").begins_with(f"TARGET#{effective_at}"),
            ConsistentRead=True,
        ).get("Items", [])
        for candidate in candidates:
            candidate_plain = _from_storage(candidate)
            if candidate_plain.get("SK") != expected["SK"]:
                return "conflict", "another target exists at the legacy effective timestamp"
        existing = self._get({"PK": expected["PK"], "SK": expected["SK"]})
        if existing:
            matching = all(existing.get(key) == expected.get(key) for key in ("entity_type", "target_id", "effective_at", "user_id", "telegram_user_id", "source", "migration_version"))
            matching = matching and all(_same_number(existing.get("target", {}).get(key), expected["target"].get(key)) for key in expected["target"])
            return ("skip" if matching else "conflict"), None if matching else "target record mismatch"
        if not dry_run:
            if not self._put_if_absent(expected):
                reread = self._get({"PK": expected["PK"], "SK": expected["SK"]})
                if reread is None:
                    raise MigrationError("target conditional write raced and could not be re-read")
                return "skip", None
        return "create", None

    def _meal_state(self, identity: Any, meal: LegacyMeal, imported_at: str, *, dry_run: bool) -> tuple[str, Optional[str]]:
        expected_meal, expected_pointer = _meal_items(identity, meal, imported_at)
        meal_existing = self._get({"PK": expected_meal["PK"], "SK": expected_meal["SK"]})
        pointer_existing = self._get({"PK": expected_pointer["PK"], "SK": expected_pointer["SK"]})
        if meal_existing is not None or pointer_existing is not None:
            if meal_existing is not None and pointer_existing is not None and _meal_matches(meal_existing, expected_meal) and _pointer_matches(pointer_existing, expected_pointer):
                return "skip", None
            return "conflict", "partial or mismatched meal/pointer record"
        if not dry_run:
            self.repository._transact_write(
                [
                    {"operation": "Put", "TableName": self.repository.table_name, "Item": expected_meal, "ConditionExpression": "attribute_not_exists(PK)"},
                    {"operation": "Put", "TableName": self.repository.table_name, "Item": expected_pointer, "ConditionExpression": "attribute_not_exists(PK)"},
                ]
            )
        return "create", None

    def _resolve_identity(self, entry: LegacyProfile, *, dry_run: bool) -> tuple[Any, str]:
        profile = entry.profile
        existing = self.identity_record(profile.telegram_user_id)
        if existing:
            if dry_run:
                return type("Identity", (), {"telegram_user_id": profile.telegram_user_id, "user_id": str(existing["user_id"]), "pk": f"USER#{existing['user_id']}"})(), "existing"
            identity = self.repository.resolve_identity(profile.telegram_user_id)
            return identity, "existing"
        if dry_run:
            return None, "would_create"
        identity = self.repository.resolve_identity(profile.telegram_user_id, username=profile.username, display_name=profile.display_name)
        return identity, "created"

    def plan_or_import(self, profiles: list[LegacyProfile], meals: list[LegacyMeal], *, dry_run: bool, imported_at: str) -> dict[str, Any]:
        identities: dict[int, Any] = {}
        identity_rows: list[dict[str, Any]] = []
        for entry in profiles:
            identity, state = self._resolve_identity(entry, dry_run=dry_run)
            if identity is not None:
                identities[entry.profile.telegram_user_id] = identity
            identity_rows.append({
                "telegram_user_id": entry.profile.telegram_user_id,
                "status": state,
                "internal_user_id": identity.user_id if identity is not None else None,
            })

        profile_rows: list[dict[str, Any]] = []
        target_rows: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for entry in profiles:
            identity = identities.get(entry.profile.telegram_user_id)
            if identity is None:
                profile_rows.append({"telegram_user_id": entry.profile.telegram_user_id, "status": "create"})
                target_rows.append({"telegram_user_id": entry.profile.telegram_user_id, "status": "create"})
                continue
            profile_state, profile_conflicts = self._merge_profile(identity, entry, imported_at, dry_run=dry_run)
            profile_rows.append({"telegram_user_id": entry.profile.telegram_user_id, "internal_user_id": identity.user_id, "status": profile_state})
            if profile_conflicts:
                conflicts.append({"category": "profile_conflict", "telegram_user_id": entry.profile.telegram_user_id, "fields": profile_conflicts})
            target_state, target_conflict = self._target_state(identity, entry, imported_at, dry_run=dry_run)
            target_rows.append({"telegram_user_id": entry.profile.telegram_user_id, "internal_user_id": identity.user_id, "status": target_state})
            if target_conflict:
                conflicts.append({"category": "target_conflict", "telegram_user_id": entry.profile.telegram_user_id, "detail": target_conflict})

        meal_rows: list[dict[str, Any]] = []
        timestamp_assumptions: set[str] = set()
        for meal in meals:
            if meal.assumed_timezone:
                timestamp_assumptions.add(LEGACY_TIMEZONE)
            identity = identities.get(meal.telegram_user_id)
            if identity is None:
                state = "create"
                conflict = None
            else:
                state, conflict = self._meal_state(identity, meal, imported_at, dry_run=dry_run)
                if conflict:
                    conflicts.append({"category": "meal_conflict", "telegram_user_id": meal.telegram_user_id, "source_row": meal.source_row, "meal_id": meal.meal_id, "detail": conflict})
            meal_rows.append({"telegram_user_id": meal.telegram_user_id, "internal_user_id": identity.user_id if identity is not None else None, "source_row": meal.source_row, "meal_id": meal.meal_id, "status": state})

        if conflicts:
            raise MigrationError(json.dumps({"conflicts": conflicts}, sort_keys=True))

        def count(states: list[Mapping[str, Any]], value: str) -> int:
            return sum(1 for row in states if row.get("status") == value)

        per_user = {
            str(telegram_id): {
                "meals": sum(1 for meal in meals if meal.telegram_user_id == telegram_id),
                "meal_create": sum(1 for row in meal_rows if row["telegram_user_id"] == telegram_id and row["status"] == "create"),
                "meal_skip": sum(1 for row in meal_rows if row["telegram_user_id"] == telegram_id and row["status"] == "skip"),
                "internal_user_id": identities[telegram_id].user_id if telegram_id in identities else None,
            }
            for telegram_id in sorted({entry.profile.telegram_user_id for entry in profiles})
        }
        return {
            "migration_version": MIGRATION_VERSION,
            "mode": "dry_run" if dry_run else "import",
            "source_profiles": len(profiles),
            "source_meals": len(meals),
            "identities": identity_rows,
            "profiles": profile_rows,
            "targets": target_rows,
            "meals": meal_rows,
            "summary": {
                "profiles_create": count(profile_rows, "create"),
                "profiles_merge": count(profile_rows, "merge"),
                "profiles_preserved": count(profile_rows, "preserve"),
                "profiles_skip": count(profile_rows, "skip"),
                "targets_create": count(target_rows, "create"),
                "targets_skip": count(target_rows, "skip"),
                "meals_create": count(meal_rows, "create"),
                "meals_skip": count(meal_rows, "skip"),
                "expected_meal_split": {"349553317": 19, "559404539": 17},
                "actual_meal_split": {str(key): value["meals"] for key, value in per_user.items()},
            },
            "per_user": per_user,
            "malformed_records": [],
            "timestamp_assumptions": sorted(timestamp_assumptions) or ["naive legacy timestamps interpreted as Asia/Singapore"],
            "detail_policy": "No historical meal detail records imported; legacy JSONL metrics are non-authoritative.",
            "source_checksums": source_checksums(),
            "conflicts": [],
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Validate and report without writing DynamoDB")
    mode.add_argument("--import", dest="do_import", action="store_true", help="Write the validated migration to DynamoDB")
    parser.add_argument("--profile", default=None, help="AWS CLI profile")
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--table-name", default=DEFAULT_TABLE_NAME)
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--meals", type=Path, default=DEFAULT_MEALS_PATH)
    parser.add_argument("--report-file", type=Path, default=None)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        profiles = load_profiles(args.profiles)
        meals, malformed = load_meals(args.meals)
        validate_sources(profiles, meals, malformed)
        import boto3

        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        table = session.resource("dynamodb").Table(args.table_name)
        runner = MigrationRunner(table, client=session.client("dynamodb"))
        report = runner.plan_or_import(
            profiles,
            meals,
            dry_run=not args.do_import,
            imported_at=utc_iso(datetime.now(timezone.utc)),
        )
        report["malformed_records"] = malformed
        if args.report_file:
            args.report_file.parent.mkdir(parents=True, exist_ok=True)
            args.report_file.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except (MigrationError, ValueError, OSError) as err:
        LOGGER.error("migration_failed category=%s detail=%s", type(err).__name__, str(err)[:300])
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
