import copy
import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from boto3.dynamodb.types import TypeDeserializer

from migrations.migrate_to_dynamodb import (
    MIGRATION_VERSION,
    MigrationError,
    MigrationRunner,
    _historical_estimate,
    _meal_items,
    _target_id,
    load_meals,
    load_profiles,
    parse_legacy_datetime,
    validate_sources,
)


class ConditionalCheckFailedException(Exception):
    pass


class _FakeTable:
    name = "fitness"

    def __init__(self):
        self.items = {}

    @staticmethod
    def _key(key):
        return key["PK"], key["SK"]

    def get_item(self, **kwargs):
        item = self.items.get(self._key(kwargs["Key"]))
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, **kwargs):
        key = self._key(kwargs["Item"])
        if key in self.items and kwargs.get("ConditionExpression"):
            raise ConditionalCheckFailedException()
        self.items[key] = copy.deepcopy(kwargs["Item"])

    def update_item(self, **kwargs):
        key = self._key(kwargs["Key"])
        if key not in self.items and kwargs.get("ConditionExpression"):
            raise ConditionalCheckFailedException()
        item = copy.deepcopy(self.items.get(key, {}))
        names = kwargs.get("ExpressionAttributeNames", {})
        values = kwargs.get("ExpressionAttributeValues", {})
        expression = kwargs["UpdateExpression"].replace("SET ", "", 1)
        for assignment in expression.split(","):
            name, value_name = [part.strip() for part in assignment.split("=", 1)]
            item[names.get(name, name)] = copy.deepcopy(values[value_name])
        self.items[key] = item

    def transact_write_items(self, **kwargs):
        deserializer = TypeDeserializer()
        pending = copy.deepcopy(self.items)
        for operation in kwargs["TransactItems"]:
            name, payload = next(iter(operation.items()))
            if name != "Put":
                raise AssertionError(f"unexpected operation: {name}")
            item = {key: deserializer.deserialize(value) for key, value in payload["Item"].items()}
            key = self._key(item)
            if key in pending and payload.get("ConditionExpression"):
                raise ConditionalCheckFailedException()
            pending[key] = item
        self.items = pending

    def query(self, **kwargs):
        expression = kwargs["KeyConditionExpression"].get_expression()
        values = expression["values"]
        pk = values[0].__dict__["_values"][1]
        prefix = values[1].__dict__["_values"][1]
        return {
            "Items": [copy.deepcopy(item) for (item_pk, item_sk), item in self.items.items() if item_pk == pk and item_sk.startswith(prefix)]
        }


def _source_data():
    root = Path(__file__).resolve().parents[1]
    profiles = load_profiles(root / "user_profiles.json")
    meals, malformed = load_meals(root / "meals_v2.csv")
    validate_sources(profiles, meals, malformed)
    return root, profiles, meals


class MigrationTests(unittest.TestCase):
    def test_sources_and_expected_split_reconcile(self):
        _, profiles, meals = _source_data()
        self.assertEqual({entry.profile.telegram_user_id for entry in profiles}, {349553317, 559404539})
        self.assertEqual(len(meals), 36)
        self.assertEqual(sum(meal.telegram_user_id == 349553317 for meal in meals), 19)
        self.assertEqual(sum(meal.telegram_user_id == 559404539 for meal in meals), 17)

    def test_deterministic_ids_and_singapore_timestamp_conversion(self):
        _, profiles, meals = _source_data()
        _, profiles_again, meals_again = _source_data()
        self.assertEqual(meals[0].meal_id, meals_again[0].meal_id)
        self.assertEqual(_target_id(profiles[0]), _target_id(profiles_again[0]))
        converted, assumed = parse_legacy_datetime("2026-03-05T00:30:00")
        self.assertTrue(assumed)
        self.assertEqual(converted.isoformat(), "2026-03-04T16:30:00+00:00")
        self.assertEqual(meals[0].eaten_at.isoformat(), "2026-03-05T05:00:00+00:00")

    def test_historical_estimate_contains_only_meal_level_values(self):
        _, _, meals = _source_data()
        estimate = _historical_estimate(meals[0])
        self.assertEqual(
            set(estimate),
            {"meal_name", "calories", "protein_g", "carbs_g", "fat_g", "confidence", "notes"},
        )
        self.assertNotIn("items", estimate)
        self.assertNotIn("variance_drivers", estimate)
        self.assertNotIn("evidence", estimate)

    def test_profile_conflict_is_reported_without_overwrite(self):
        _, profiles, meals = _source_data()
        table = _FakeTable()
        table.items[("IDENTITY#TELEGRAM#349553317", "USER")] = {
            "PK": "IDENTITY#TELEGRAM#349553317",
            "SK": "USER",
            "user_id": "existing-user",
            "telegram_user_id": 349553317,
        }
        table.items[("USER#existing-user", "PROFILE")] = {
            "PK": "USER#existing-user",
            "SK": "PROFILE",
            "daily_target": {"calories": 999, "protein_g": 1, "carbs_g": 1, "fat_g": 1},
            "display_name": "Vaan",
        }
        runner = MigrationRunner(table, now_fn=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc))
        with self.assertRaises(MigrationError) as raised:
            runner.plan_or_import(profiles, meals, dry_run=True, imported_at="2026-09-02T00:00:00Z")
        self.assertIn("profile_conflict", str(raised.exception))
        self.assertEqual(table.items[("USER#existing-user", "PROFILE")]["daily_target"]["calories"], 999)

    def test_partial_meal_or_pointer_is_a_hard_conflict(self):
        _, profiles, meals = _source_data()
        table = _FakeTable()
        table.items[("IDENTITY#TELEGRAM#349553317", "USER")] = {
            "PK": "IDENTITY#TELEGRAM#349553317",
            "SK": "USER",
            "user_id": "existing-user",
            "telegram_user_id": 349553317,
        }
        identity = type("Identity", (), {"pk": "USER#existing-user", "user_id": "existing-user", "telegram_user_id": 349553317})()
        _, pointer = _meal_items(identity, meals[0], "2026-09-02T00:00:00Z")
        table.items[(pointer["PK"], pointer["SK"])] = pointer
        runner = MigrationRunner(table, now_fn=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc))
        with self.assertRaises(MigrationError) as raised:
            runner.plan_or_import(profiles, meals, dry_run=True, imported_at="2026-09-02T00:00:00Z")
        self.assertIn("meal_conflict", str(raised.exception))

    def test_first_import_reuses_existing_identity_and_second_run_is_idempotent(self):
        _, profiles, meals = _source_data()
        table = _FakeTable()
        table.items[("IDENTITY#TELEGRAM#349553317", "USER")] = {
            "PK": "IDENTITY#TELEGRAM#349553317",
            "SK": "USER",
            "entity_type": "identity",
            "user_id": "existing-user",
            "telegram_user_id": 349553317,
            "username": "Vaanasaurus",
            "display_name": "Vaan",
        }
        runner = MigrationRunner(table, now_fn=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc))
        first = runner.plan_or_import(profiles, meals, dry_run=False, imported_at="2026-09-02T00:00:00Z")
        self.assertEqual(first["summary"]["meals_create"], 36)
        self.assertEqual(first["summary"]["meals_skip"], 0)
        self.assertEqual(table.items[("IDENTITY#TELEGRAM#349553317", "USER")]["user_id"], "existing-user")
        self.assertEqual(len([key for key in table.items if key[0].startswith("IDENTITY#")]), 2)
        self.assertEqual(sum(key[1].startswith("MEAL#") for key in table.items if key[0] == "USER#existing-user"), 19)

        second = runner.plan_or_import(profiles, meals, dry_run=False, imported_at="2026-09-02T00:01:00Z")
        self.assertEqual(second["summary"]["meals_create"], 0)
        self.assertEqual(second["summary"]["meals_skip"], 36)
        self.assertEqual(second["summary"]["targets_create"], 0)
        self.assertEqual(second["summary"]["targets_skip"], 2)
        self.assertEqual(second["summary"]["profiles_create"], 0)
        self.assertEqual(second["summary"]["profiles_preserved"], 2)
        self.assertEqual(len([key for key in table.items if key[1].startswith("MEAL_ID#")]), 36)

    def test_source_metrics_checksum_is_not_mutated_by_migration_logic(self):
        root, profiles, meals = _source_data()
        path = root / "metrics" / "estimates.jsonl"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        runner = MigrationRunner(_FakeTable(), now_fn=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc))
        report = runner.plan_or_import(profiles, meals, dry_run=True, imported_at="2026-09-02T00:00:00Z")
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(report["migration_version"], MIGRATION_VERSION)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
