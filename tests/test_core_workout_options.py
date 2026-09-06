import copy
import unittest

from macro_bot.serverless_data import DynamoNutritionRepository, ProgrammeSeedConflict
from macro_bot.serverless_service import NutritionService
from macro_bot.workout_execution import InvalidWorkoutInput, WorkoutNotFound
from macro_bot.workout_programme import CORE_OPTIONS_VERSION_ID, INITIAL_VERSION_ID, core_options_programme_records
from tests.test_serverless_data import _FakeTable


class CoreWorkoutOptionsTests(unittest.TestCase):
    def setUp(self):
        self.table = _FakeTable()
        self.repo = DynamoNutritionRepository(self.table, table_name="fitness")
        self.repo.seed_workout_programme()
        self.service = NutritionService(self.repo)

    def test_publish_is_atomic_idempotent_and_preserves_previous_version_and_sessions(self):
        owner = self.repo.resolve_identity(101)
        old_session = self.service.start_workout(owner, "PUSH")
        before = copy.deepcopy(self.table.items)
        report = self.repo.publish_core_options_programme(dry_run=True)
        self.assertTrue(report["activate"])
        self.assertEqual(self.table.items, before)
        self.repo.publish_core_options_programme()
        for key, value in before.items():
            if key not in [("PROGRAM#javaanfitness", "META"), ("PROGRAM#javaanfitness", "ACTIVE")]:
                self.assertEqual(self.table.items[key], value)
        published = copy.deepcopy(self.table.items)
        self.assertEqual(self.repo.publish_core_options_programme()["created"], 0)
        self.assertEqual(self.table.items, published)
        self.assertEqual(self.service.active_workout(owner), old_session)
        old = self.repo.get_workout_programme(INITIAL_VERSION_ID)
        self.assertNotIn("russian_twist", old["days"][0]["prescriptions"][-1]["allowed_exercise_ids"])
        current = self.repo.get_workout_programme()
        self.assertEqual(current["version"]["version_id"], CORE_OPTIONS_VERSION_ID)
        for day in current["days"]:
            core = day["prescriptions"][-1]
            self.assertIn("standing_ab_crunch_machine", core["allowed_exercise_ids"])
            self.assertIn("russian_twist", core["allowed_exercise_ids"])

    def test_conflicting_publication_writes_nothing(self):
        desired = core_options_programme_records()[-1]
        self.table.items[(desired["PK"], desired["SK"])] = {**desired, "canonical_name": "Conflict"}
        before = copy.deepcopy(self.table.items)
        with self.assertRaises(ProgrammeSeedConflict):
            self.repo.publish_core_options_programme()
        self.assertEqual(self.table.items, before)

    def test_new_core_choices_save_bodyweight_and_weighted_sets_and_resume(self):
        self.repo.publish_core_options_programme()
        for user_id, choice in [(101, "standing_ab_crunch_machine"), (202, "russian_twist")]:
            owner = self.repo.resolve_identity(user_id)
            started = self.service.start_workout(owner, "PUSH")
            session_id = started["session"]["session_id"]
            core = started["executions"][-1]
            chosen = self.service.choose_workout_exercise(owner, session_id, core["execution_id"],
                {"performed_exercise_id": choice, "expected_revision": 1})
            self.assertEqual(chosen["executions"][-1]["execution_type"], "optional_load_reps")
            first = self.service.put_workout_set(owner, session_id, core["execution_id"], 1,
                {"reps": 8, "execution_expected_revision": 2})
            self.assertIsNone(first["executions"][-1]["sets"][0]["load_value"])
            self.assertEqual(first["executions"][-1]["sets"][0]["load_scope"], "bodyweight")
            self.service.put_workout_set(owner, session_id, core["execution_id"], 2,
                {"reps": 12, "load_value": 5, "execution_expected_revision": 3})
            resumed = NutritionService(self.repo).active_workout(owner)
            self.assertEqual(resumed["executions"][-1]["sets"][1]["load_value"], 5)
            self.assertEqual(resumed["executions"][-1]["sets"][1]["load_scope"], "equipment")
            with self.assertRaises(WorkoutNotFound):
                self.service.put_workout_set(self.repo.resolve_identity(999), session_id, core["execution_id"], 1, {"reps": 1})

    def test_optional_weight_validation_does_not_relax_loaded_exercises(self):
        validate = self.service.workout_execution._validate_set_payload
        for load in (None, 0):
            result = validate({"execution_type": "optional_load_reps"}, {"load_value": load, "reps": 8})
            self.assertEqual(result["load_scope"], "bodyweight")
            self.assertIsNone(result["load_value"])
        for load in (-1, True, "bad", float("nan"), float("inf")):
            with self.assertRaises(InvalidWorkoutInput):
                validate({"execution_type": "optional_load_reps"}, {"load_value": load, "reps": 8})
        with self.assertRaises(InvalidWorkoutInput):
            validate({"execution_type": "loaded_reps"}, {"reps": 8})


if __name__ == "__main__":
    unittest.main()
