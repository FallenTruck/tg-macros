import copy
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from lambda_handlers import api
from macro_bot.serverless_data import DynamoNutritionRepository, ProgrammeSeedConflict
from macro_bot.serverless_service import NutritionService
from macro_bot.workout_programme import (
    EXERCISE_CATALOGUE,
    INITIAL_VERSION_ID,
    PROGRAMME_ID,
    initial_programme_records,
)
from tests.test_serverless_auth import _init_data
from tests.test_serverless_data import _FakeTable


class WorkoutProgrammeTests(unittest.TestCase):
    def setUp(self):
        self.table = _FakeTable()
        self.repo = DynamoNutritionRepository(self.table, table_name="fitness")
        self.service = NutritionService(self.repo)

    def test_initial_shared_programme_is_deterministic_and_has_one_active_version(self):
        records = initial_programme_records()
        self.assertEqual(PROGRAMME_ID, "javaanfitness")
        self.assertEqual(sum(item.get("entity_type") == "exercise" for item in records), 15)
        self.assertEqual(sum(item.get("entity_type") == "workout_programme_version" for item in records), 1)
        active = [item for item in records if item.get("entity_type") == "workout_programme_active_pointer"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["active_version_id"], INITIAL_VERSION_ID)
        self.assertEqual(records, initial_programme_records())

    def test_seed_is_dry_run_idempotent_and_conflict_safe(self):
        dry_run = self.repo.seed_workout_programme(dry_run=True)
        self.assertEqual(dry_run["would_create"], len(initial_programme_records()))
        first = self.repo.seed_workout_programme()
        second = self.repo.seed_workout_programme()
        self.assertEqual(first["created"], len(initial_programme_records()))
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["existing"], len(initial_programme_records()))

        key = ("PROGRAM#javaanfitness", "ACTIVE")
        self.table.items[key]["active_version_id"] = "unexpected-version"
        with self.assertRaises(ProgrammeSeedConflict):
            self.repo.seed_workout_programme(dry_run=True)

    def test_programme_days_are_ordered_and_planned_weekday_is_metadata_only(self):
        self.repo.seed_workout_programme()
        programme = self.repo.get_workout_programme()
        self.assertEqual([day["day_code"] for day in programme["days"]], ["PULL", "SUPPORT_CORE", "PUSH"])
        self.assertEqual([day["planned_weekday"] for day in programme["days"]], ["TUESDAY", "FRIDAY", "SUNDAY"])
        self.assertIn("actual sessions may occur", programme["days"][0]["notes"])
        self.assertEqual(self.repo.get_workout_programme_day("push")["day"]["day_code"], "PUSH")
        self.assertEqual(self.repo.get_workout_programme(INITIAL_VERSION_ID)["version"]["version_id"], INITIAL_VERSION_ID)

    def test_approved_day_prescriptions_choices_optional_and_execution_types(self):
        self.repo.seed_workout_programme()
        programme = self.repo.get_workout_programme()
        days = {day["day_code"]: day for day in programme["days"]}
        self.assertEqual([item["display_label"] for item in days["PULL"]["prescriptions"]], [
            "Lat Pulldown", "Seated Cable Row", "Rear-delt accessory", "Dumbbell Biceps Curl", "Optional Core"
        ])
        pull_core = days["PULL"]["prescriptions"][-1]
        self.assertTrue(pull_core["optional"])
        self.assertEqual(pull_core["allowed_exercise_ids"], ["pallof_press", "dead_bug"])
        support_core = days["SUPPORT_CORE"]["prescriptions"][-1]
        targets = support_core["option_targets"]
        self.assertEqual(targets["pallof_press"]["execution_type"], "side_aware_reps")
        self.assertEqual(targets["dead_bug"]["execution_type"], "bodyweight_reps")
        self.assertEqual(targets["side_plank"]["execution_type"], "timed")
        self.assertNotIn("rep_min", targets["side_plank"])
        self.assertEqual(days["PUSH"]["prescriptions"][0]["allowed_exercise_ids"], ["flat_dumbbell_chest_press", "incline_dumbbell_chest_press"])

    def test_catalogue_exposes_dumbbell_convention_and_unconfigured_loads(self):
        by_id = {item["exercise_id"]: item for item in EXERCISE_CATALOGUE}
        self.assertEqual(by_id["flat_dumbbell_chest_press"]["loading_convention"], "per_dumbbell_kg")
        self.assertEqual(by_id["pallof_press"]["unilateral_mode"], "side_independent")
        self.assertEqual(by_id["side_plank"]["execution_type"], "timed")
        self.assertEqual(by_id["dead_bug"]["execution_type"], "bodyweight_reps")
        self.assertEqual(by_id["lat_pulldown"]["load_configuration"], {"configured": False})

    def test_no_user_execution_or_progression_state_is_in_shared_records(self):
        for item in initial_programme_records():
            self.assertNotIn("user_id", item)
            self.assertNotIn("telegram_user_id", item)
            self.assertNotIn("session_id", item)
            self.assertNotIn("load_kg", item)
            self.assertNotIn("progression_state", item)

    def test_both_users_receive_the_same_shared_programme(self):
        self.repo.seed_workout_programme()
        first = self.service.workout_programme()
        second = self.service.workout_programme()
        self.assertEqual(first, second)

    def test_authenticated_api_returns_programme_and_rejects_missing_auth(self):
        self.repo.seed_workout_programme()
        client = TestClient(api.app)
        valid = _init_data()
        with patch.object(api, "_service", return_value=self.service), patch.object(
            api, "bot_token_from_environment", return_value="test-token"
        ):
            response = client.get("/api/workout/programme", headers={"X-Telegram-Init-Data": valid})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["version"]["version_id"], INITIAL_VERSION_ID)
            self.assertEqual(len(response.json()["days"]), 3)
            day = client.get("/api/workout/programme/days/SUPPORT_CORE", headers={"X-Telegram-Init-Data": valid})
            self.assertEqual(day.status_code, 200)
            self.assertEqual(day.json()["day"]["day_code"], "SUPPORT_CORE")
            denied = client.get("/api/workout/programme")
            self.assertEqual(denied.status_code, 401)

    def test_seed_does_not_contain_nutrition_records_or_change_existing_items(self):
        nutrition_key = ("USER#existing", "PROFILE")
        self.table.items[nutrition_key] = {"PK": nutrition_key[0], "SK": nutrition_key[1], "entity_type": "profile", "daily_target": {"calories": 2200}}
        before = copy.deepcopy(self.table.items[nutrition_key])
        self.repo.seed_workout_programme()
        self.assertEqual(self.table.items[nutrition_key], before)
        self.assertTrue(all(item["PK"].startswith(("PROGRAM#", "CATALOG#")) for key, item in self.table.items.items() if key != nutrition_key))


if __name__ == "__main__":
    unittest.main()
