import csv
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import List

from .config import CSV_FIELDNAMES
from .identity import legacy_identity_for_person, map_legacy_people_to_user_ids
from .models import (
    CatalogSuggestion,
    DailyMacroSummary,
    FoodCatalogEntry,
    LoggedMealRow,
    MacroTotal,
    UserProfile,
)

logger = logging.getLogger(__name__)


class MealLogRepository:
    def __init__(self, meals_v2_csv_path: Path, legacy_meals_csv_path: Path):
        self._target_path = meals_v2_csv_path
        self._legacy_path = legacy_meals_csv_path
        self._check_legacy_schema()

    def _check_legacy_schema(self) -> None:
        if not self._legacy_path.exists():
            return

        with self._legacy_path.open("r", newline="", encoding="utf-8") as csvfile:
            reader = csv.reader(csvfile)
            header = next(reader, [])

        if "person" not in header:
            logger.warning(
                "Legacy meals.csv detected without person column. "
                "Using meals_v2.csv as canonical write target."
            )

    def append(self, row: LoggedMealRow) -> None:
        file_exists = self._target_path.exists()
        with self._target_path.open("a", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=CSV_FIELDNAMES)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row.to_csv_row())

    def list_meals_for_user(self, telegram_user_id: int) -> List[LoggedMealRow]:
        return [row for row in self._load_all_rows() if row.telegram_user_id == telegram_user_id]

    def list_all_meals(self) -> List[LoggedMealRow]:
        return self._load_all_rows()

    def list_meals_for_user_on_date(self, telegram_user_id: int, target_date: date) -> List[LoggedMealRow]:
        return [
            row
            for row in self.list_meals_for_user(telegram_user_id)
            if row.logged_at.date() == target_date
        ]

    def list_recent_meals(self, telegram_user_id: int, limit: int = 5) -> List[LoggedMealRow]:
        rows = sorted(self.list_meals_for_user(telegram_user_id), key=lambda item: item.logged_at)
        return rows[-limit:]

    def get_daily_summary(self, telegram_user_id: int, target_date: date) -> DailyMacroSummary:
        meals = sorted(
            self.list_meals_for_user_on_date(telegram_user_id, target_date),
            key=lambda item: item.logged_at,
        )
        totals = MacroTotal(
            calories=sum(meal.calories for meal in meals),
            protein_g=sum(meal.protein_g for meal in meals),
            carbs_g=sum(meal.carbs_g for meal in meals),
            fat_g=sum(meal.fat_g for meal in meals),
        )
        return DailyMacroSummary(
            telegram_user_id=telegram_user_id,
            date_iso=target_date.isoformat(),
            totals=totals,
            meals=meals,
        )

    def _load_all_rows(self) -> List[LoggedMealRow]:
        if not self._target_path.exists():
            return []
        with self._target_path.open("r", newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            rows: List[LoggedMealRow] = []
            for raw_row in reader:
                try:
                    rows.append(LoggedMealRow.from_csv_row(raw_row))
                except Exception as err:
                    logger.warning(
                        "Skipping malformed meal row in %s detail=%s row=%s",
                        self._target_path.name,
                        str(err)[:120],
                        raw_row,
                    )
            return rows


class UserProfileStore:
    def __init__(self, profile_path: Path):
        self._profile_path = profile_path

    def get(self, telegram_user_id: int) -> UserProfile:
        profiles = self._load_profiles()
        user_id = int(telegram_user_id)
        if user_id not in profiles:
            raise KeyError(f"Unknown profile: {user_id}")
        return profiles[user_id]

    def list_profiles(self) -> List[UserProfile]:
        return list(self._load_profiles().values())

    def upsert(self, profile: UserProfile) -> None:
        payload = self._load_payload()
        profiles = [
            item
            for item in payload.get("profiles", [])
            if int(item.get("telegram_user_id", -1)) != profile.telegram_user_id
        ]
        profiles.append(profile.to_payload())
        profiles.sort(key=lambda item: int(item["telegram_user_id"]))
        self._write_payload({"profiles": profiles})

    def _load_profiles(self) -> dict[int, UserProfile]:
        payload = self._load_payload()
        return {
            int(item["telegram_user_id"]): UserProfile.from_payload(item)
            for item in payload.get("profiles", [])
        }

    def _load_payload(self) -> dict:
        if not self._profile_path.exists():
            return {"profiles": []}
        with self._profile_path.open("r", encoding="utf-8") as profile_file:
            payload = json.load(profile_file)
        migrated = self._migrate_legacy_payload(payload)
        if migrated != payload:
            self._write_payload(migrated)
        return migrated

    def _write_payload(self, payload: dict) -> None:
        self._profile_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _migrate_legacy_payload(self, payload: dict) -> dict:
        profiles = payload.get("profiles", [])
        if not profiles:
            return {"profiles": []}
        if all("telegram_user_id" in item for item in profiles):
            return {"profiles": profiles}

        migrated = []
        timestamp = datetime.utcnow().isoformat(timespec="seconds")
        for item in profiles:
            if "telegram_user_id" in item:
                migrated.append(item)
                continue
            identity = legacy_identity_for_person(str(item.get("person", "")))
            if identity is None:
                raise ValueError(f"Cannot migrate legacy profile for person={item.get('person')}")
            migrated.append(
                {
                    "telegram_user_id": identity.telegram_user_id,
                    "username": identity.username,
                    "display_name": str(item.get("display_name") or identity.display_name),
                    "daily_target": item["daily_target"],
                    "questionnaire_answers": None,
                    "questionnaire_version": "legacy-migrated",
                    "updated_at": timestamp,
                    "dietary_preferences": list(item.get("dietary_preferences", [])),
                    "restrictions": list(item.get("restrictions", [])),
                    "preferred_cuisines": list(item.get("preferred_cuisines", [])),
                    "preferred_staples": list(item.get("preferred_staples", [])),
                    "preferred_tags": list(item.get("preferred_tags", [])),
                }
            )
        migrated.sort(key=lambda item: int(item["telegram_user_id"]))
        return {"profiles": migrated}


class FoodCatalogStore:
    def __init__(self, catalog_path: Path):
        self._catalog_path = catalog_path

    def list_entries(self) -> List[FoodCatalogEntry]:
        payload = self._load_payload()
        return [FoodCatalogEntry.from_payload(item) for item in payload.get("foods", [])]

    def append_entries(self, entries: List[FoodCatalogEntry]) -> int:
        if not entries:
            return 0

        payload = self._load_payload()
        foods = payload.setdefault("foods", [])
        existing_ids = {str(item.get("food_id")) for item in foods}
        appended = 0
        for entry in entries:
            if entry.food_id in existing_ids:
                continue
            foods.append(entry.to_payload())
            existing_ids.add(entry.food_id)
            appended += 1

        if appended:
            self._catalog_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return appended

    def _load_payload(self) -> dict:
        with self._catalog_path.open("r", encoding="utf-8") as catalog_file:
            payload = json.load(catalog_file)
        migrated = self._migrate_legacy_payload(payload)
        if migrated != payload:
            self._catalog_path.write_text(json.dumps(migrated, indent=2), encoding="utf-8")
        return migrated

    @staticmethod
    def _migrate_legacy_payload(payload: dict) -> dict:
        foods = payload.get("foods", [])
        if not foods or all("eligible_telegram_user_ids" in item for item in foods):
            return payload

        migrated = []
        for item in foods:
            migrated_item = dict(item)
            migrated_item["eligible_telegram_user_ids"] = map_legacy_people_to_user_ids(
                [str(x) for x in item.get("people", [])]
            )
            migrated_item.pop("people", None)
            migrated.append(migrated_item)
        return {"foods": migrated}


class CatalogSuggestionStore:
    def __init__(self, suggestion_path: Path):
        self._suggestion_path = suggestion_path

    def list(self) -> List[CatalogSuggestion]:
        payload = self._load_payload()
        return [CatalogSuggestion.from_payload(item) for item in payload.get("suggestions", [])]

    def save(self, suggestions: List[CatalogSuggestion]) -> None:
        payload = {
            "suggestions": [item.to_payload() for item in suggestions],
        }
        self._suggestion_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load_payload(self) -> dict:
        if not self._suggestion_path.exists():
            return {"suggestions": []}
        with self._suggestion_path.open("r", encoding="utf-8") as suggestion_file:
            payload = json.load(suggestion_file)
        migrated = self._migrate_legacy_payload(payload)
        if migrated != payload:
            self._suggestion_path.write_text(json.dumps(migrated, indent=2), encoding="utf-8")
        return migrated

    @staticmethod
    def _migrate_legacy_payload(payload: dict) -> dict:
        suggestions = payload.get("suggestions", [])
        if not suggestions or all("telegram_user_id" in item for item in suggestions):
            return payload

        migrated = []
        for item in suggestions:
            identity = legacy_identity_for_person(str(item.get("person", "")))
            if identity is None:
                raise ValueError(f"Cannot migrate legacy suggestion for person={item.get('person')}")
            migrated_item = dict(item)
            migrated_item["telegram_user_id"] = identity.telegram_user_id
            migrated_item["eligible_telegram_user_ids"] = map_legacy_people_to_user_ids(
                [str(x) for x in item.get("people", [])]
            )
            migrated_item.pop("person", None)
            migrated_item.pop("people", None)
            migrated.append(migrated_item)
        migrated.sort(key=lambda item: (int(item["telegram_user_id"]), str(item["suggestion_id"])))
        return {"suggestions": migrated}
