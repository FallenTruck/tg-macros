"""Application behavior exercised without DynamoDB or a nutrition repository."""
import ast
import copy
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from macro_bot.workout_repository import WorkoutRepository
from macro_bot.workout_service import WorkoutService
from macro_bot.workout_types import InvalidWorkoutInput, WorkoutConflict, WorkoutNotFound


class WorkoutServiceTests(unittest.TestCase):
    def setUp(self):
        self.identity = SimpleNamespace(user_id="user")
        self.session = {"session_id": "session", "revision": 1, "status": "in_progress"}
        self.execution = {
            "execution_id": "session:001", "prescription_sequence": 1,
            "revision": 1, "status": "pending", "execution_type": "loaded_reps",
            "prescribed_set_count_min": 2, "performed_exercise_id": "a",
            "allowed_exercise_ids": ["a", "b"], "programme_version_id": "v1",
            "option_targets": {"b": {"rep_min": 8, "rep_max": 12, "set_min": 3}},
        }
        self.repository = Mock(spec_set=WorkoutRepository)
        self.repository.get_session.side_effect = lambda *_: copy.deepcopy(self.session)
        self.repository.get_execution.side_effect = lambda *_: copy.deepcopy(self.execution)
        self.repository.get_executions.side_effect = lambda *_: [copy.deepcopy(self.execution)]
        self.repository.get_sets.return_value = []
        self.repository.get_set.return_value = None
        self.repository.get_active_session.return_value = (False, None)
        self.programme = Mock(return_value={"exercises": [{"exercise_id": "b", "execution_type": "bodyweight_reps"}]})
        self.service = WorkoutService(self.repository, self.programme,
            now_fn=lambda: datetime(2026, 9, 13, 12, 0, 0, 123456, tzinfo=timezone.utc),
            session_id_factory=lambda: "session")

    def test_closed_sessions_reject_every_mutation_before_writing(self):
        for status in ("cancelled", "completed"):
            self.session["status"] = status
            actions = [
                lambda: self.service.put_set(self.identity, "session", "session:001", 1, {}),
                lambda: self.service.skip_set(self.identity, "session", "session:001", 1, {}),
                lambda: self.service.select_exercise(self.identity, "session", "session:001", {}),
                lambda: self.service.skip_execution(self.identity, "session", "session:001", {}),
                lambda: self.service.reset_execution(self.identity, "session", "session:001", {}),
                lambda: self.service.complete_session(self.identity, "session", {}),
                lambda: self.service.cancel_session(self.identity, "session", {}),
            ]
            for action in actions:
                with self.subTest(status=status, action=action), self.assertRaisesRegex(WorkoutConflict, "not in progress"):
                    action()
        for method in ("save_set", "update_execution", "complete_session", "cancel_session"):
            getattr(self.repository, method).assert_not_called()

    def test_completion_requires_resolved_working_slots(self):
        self.repository.get_sets.return_value = [{"set_type": "warmup", "status": "completed"}, {"status": "completed"}]
        with self.assertRaisesRegex(WorkoutConflict, "incomplete: 1"):
            self.service.complete_session(self.identity, "session", {"expected_revision": 1})
        self.repository.complete_session.assert_not_called()
        self.repository.get_sets.return_value.append({"set_type": "working", "status": "skipped"})
        self.service.complete_session(self.identity, "session", {"expected_revision": 1})
        args = self.repository.complete_session.call_args.args
        self.assertEqual(args[2], [self.execution])
        self.assertEqual(args[3:], (1, "2026-09-13T12:00:00Z"))

    def test_completion_passes_skipped_revisions_to_repository(self):
        self.execution.update(status="skipped", revision=7)
        self.service.complete_session(self.identity, "session", {"expected_revision": 1})
        self.repository.get_sets.assert_called_once()  # Response assembly only.
        self.assertEqual(self.repository.complete_session.call_args.args[2][0]["revision"], 7)

    def test_skip_and_reset_retain_sets_and_advance_expected_revision(self):
        def update(_identity, _session, _execution, fields, expected, **kwargs):
            self.assertEqual(expected, self.execution["revision"])
            self.execution.update(fields)
        self.repository.update_execution.side_effect = update
        self.repository.get_sets.return_value = [{"set_ordinal": 1, "reps": 8}]
        skipped = self.service.skip_execution(self.identity, "session", "session:001", {"expected_revision": 1})
        self.assertEqual(skipped["executions"][0]["status"], "skipped")
        with self.assertRaisesRegex(WorkoutConflict, "must be reset"):
            self.service.put_set(self.identity, "session", "session:001", 2, {"reps": 8})
        self.service.skip_execution(self.identity, "session", "session:001", {})
        self.assertEqual(self.repository.update_execution.call_count, 1)
        result = self.service.reset_execution(self.identity, "session", "session:001", {"expected_revision": 2})
        self.assertEqual(result["executions"][0]["status"], "pending")
        self.assertEqual(result["executions"][0]["revision"], 3)
        self.assertEqual(result["executions"][0]["sets"], [{"set_ordinal": 1, "reps": 8}])

    def test_selection_uses_saved_version_and_option_targets(self):
        self.service.select_exercise(self.identity, "session", "session:001", {"performed_exercise_id": "b", "expected_revision": 1})
        self.programme.assert_called_once_with(version_id="v1")
        fields = self.repository.update_execution.call_args.args[3]
        self.assertEqual(fields["prescribed_set_count_min"], 3)
        self.assertEqual(fields["execution_type"], "bodyweight_reps")
        self.assertEqual(fields["revision"], 2)
        self.repository.update_execution.reset_mock()
        self.repository.get_sets.return_value = [{"set_ordinal": 1}]
        with self.assertRaisesRegex(WorkoutConflict, "after sets"):
            self.service.select_exercise(self.identity, "session", "session:001", {"performed_exercise_id": "b", "expected_revision": 1})
        self.repository.update_execution.assert_not_called()
        with self.assertRaises(InvalidWorkoutInput):
            self.service.select_exercise(self.identity, "session", "session:001", {"performed_exercise_id": "unknown"})

    def test_save_validates_then_passes_both_revisions_and_reconstructs(self):
        self.repository.get_set.return_value = {"revision": 2, "created_at": "earlier"}
        def save(*args):
            self.repository.get_sets.return_value = [args[3]]
        self.repository.save_set.side_effect = save
        result = self.service.put_set(self.identity, "session", "session:001", 1,
            {"load_value": 20.5, "reps": 8, "expected_revision": 2, "execution_expected_revision": 1})
        args = self.repository.save_set.call_args.args
        self.assertEqual(args[4:], (2, 1, True))
        self.assertEqual(args[3]["revision"], 3)
        self.assertEqual(args[3]["created_at"], "earlier")
        self.assertEqual(result["executions"][0]["sets"][0]["load_value"], 20.5)
        self.repository.save_set.reset_mock()
        with self.assertRaises(WorkoutConflict):
            self.service.put_set(self.identity, "session", "session:001", 1, {"load_value": 20, "reps": 8, "expected_revision": 1})
        self.repository.save_set.assert_not_called()

    def test_start_uses_one_snapshot_and_returns_application_records(self):
        self.programme.return_value = {
            "programme": {"programme_id": "p"}, "version": {"version_id": "v1"},
            "days": [{"day_code": "PULL", "prescriptions": [{"sequence": 1, "default_exercise_id": "a", "set_min": 2}]}],
            "exercises": [],
        }
        result = self.service.start_session(self.identity, "pull", actual_local_date="2026-09-13")
        self.programme.assert_called_once_with()
        self.assertEqual(result["session"]["programme_version_id"], "v1")
        self.assertEqual(result["session"]["started_at"], "2026-09-13T12:00:00Z")
        for record in [self.repository.create_session.call_args.args[1], *self.repository.create_session.call_args.args[2]]:
            self.assertFalse({"PK", "SK", "entity_type"} & record.keys())

    def test_dangling_pointer_and_missing_session_keep_errors(self):
        self.repository.get_active_session.return_value = (True, None)
        with self.assertRaisesRegex(WorkoutConflict, "pointer is inconsistent"):
            self.service.start_session(self.identity, "PULL", actual_local_date="2026-09-13")
        self.assertIsNone(self.service.get_active_session(self.identity))
        self.repository.get_session.side_effect = None
        self.repository.get_session.return_value = None
        with self.assertRaises(WorkoutNotFound):
            self.service.get_session(self.identity, "missing")

    def test_service_has_no_storage_or_nutrition_dependencies(self):
        source = (Path(__file__).resolve().parents[1] / "macro_bot/workout_service.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, ("serverless_data", "dynamo_store", "dynamo_workout_repository", "dynamo_programme_repository"))
                self.assertFalse((node.module or "").startswith(("boto3", "botocore")))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn(node.value, ("PK", "SK", "ConditionExpression", "TableName"))
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and node.value.attr == "repository":
                self.assertFalse(node.attr.startswith("_"))
