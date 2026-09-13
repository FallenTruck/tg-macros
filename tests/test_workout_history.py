import base64
import copy
import json
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from lambda_handlers import api
from macro_bot.workout_execution import InvalidWorkoutInput, WorkoutNotFound, WorkoutConflict
from scripts.backfill_workout_history import backfill
from tests import test_workout_execution as fixtures
from tests.test_serverless_data import _FakeTable


class HistoryTable(_FakeTable):
    def query(self, **kwargs):
        assert "FilterExpression" not in kwargs
        # Every history/detail request must constrain its sort key.
        expression = kwargs["KeyConditionExpression"].get_expression()
        assert expression["operator"] == "AND" or not str(expression["values"][1]).startswith("USER#")
        full = {k: v for k, v in kwargs.items() if k not in {"Limit", "ExclusiveStartKey"}}
        items = super().query(**full)["Items"]
        start = kwargs.get("ExclusiveStartKey")
        if start:
            items = [i for i in items if i["SK"] < start["SK"]] if not kwargs.get("ScanIndexForward", True) else [i for i in items if i["SK"] > start["SK"]]
        limit = kwargs.get("Limit", len(items))
        result = {"Items": items[:limit]}
        if len(items) > limit:
            result["LastEvaluatedKey"] = {k: items[limit-1][k] for k in ("PK", "SK")}
        return result

    def scan(self, **kwargs):
        raise AssertionError("Request must never scan")


class WorkoutHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkoutExecutionTests()
        self.fixture.setUp()
        self.service = self.fixture.service
        self.repo = self.fixture.repo
        self.identity = self.fixture.identity()
        self.other = self.fixture.identity(202)
        self.table = self.fixture.table
        self.table.__class__ = HistoryTable

    def close(self, completed=False):
        payload = self.service.start_workout(self.identity, "PULL")
        sid = payload["session"]["session_id"]
        if completed:
            for ex in payload["executions"]:
                self.service.skip_workout_exercise(self.identity, sid, ex["execution_id"], {"expected_revision": 1})
            self.service.complete_workout(self.identity, sid, {"expected_revision": 1})
        else:
            self.service.put_workout_set(self.identity, sid, payload["executions"][0]["execution_id"], 1, {"load_value": 40, "reps": 8, "rir": 2})
            self.service.cancel_workout(self.identity, sid, {"expected_revision": 1})
        return sid

    def test_order_pagination_ownership_and_no_child_reads(self):
        self.table.put_item(Item={"PK": self.identity.pk, "SK": "MEAL#unrelated", "entity_type": "meal"})
        first = self.close(True)
        self.close()  # Cancelled entries between visible pages.
        second = self.close(True)
        self.close()  # Newest entry must not consume a visible slot.
        self.service.start_workout(self.identity, "PULL")
        with patch.object(self.repo.store, "query", side_effect=AssertionError("List cannot load children")):
            page = self.service.list_workout_history(self.identity, limit=1)
            self.assertEqual([s["session_id"] for s in page["sessions"]], [second])
            self.assertEqual(page["sessions"][0]["status"], "completed")
            self.assertEqual(page["sessions"][0]["duration_minutes"], 0)
            rest = self.service.list_workout_history(self.identity, limit=1, cursor=page["next_cursor"])
            self.assertEqual(rest["sessions"][0]["session_id"], first)
            self.assertEqual(rest["sessions"][0]["status"], "completed")
            self.assertIsNone(rest["next_cursor"])
            self.assertEqual(self.service.list_workout_history(self.other)["sessions"], [])
            with self.assertRaises(InvalidWorkoutInput):
                self.service.list_workout_history(self.other, cursor=page["next_cursor"])

    def test_start_timestamp_precedes_session_id_and_duration(self):
        from datetime import timedelta
        self.service.workout_execution.session_id_factory = lambda: "z-old"
        self.close(True)
        self.fixture.now += timedelta(days=1)
        self.service.workout_execution.session_id_factory = lambda: "a-new"
        payload = self.service.start_workout(self.identity, "PULL")
        self.fixture.now += timedelta(minutes=71)
        for ex in payload["executions"]:
            self.service.skip_workout_exercise(self.identity, payload["session"]["session_id"], ex["execution_id"], {"expected_revision": 1})
        self.service.complete_workout(self.identity, payload["session"]["session_id"], {"expected_revision": 1})
        page = self.service.list_workout_history(self.identity, limit=50)
        self.assertEqual([s["session_id"] for s in page["sessions"]], ["a-new", "z-old"])
        self.assertEqual(page["sessions"][0]["duration_minutes"], 71)

    def test_detail_saved_sets_skips_and_immutable_pointer(self):
        cancelled = self.close()
        completed = self.close(True)
        self.service.start_workout(self.identity, "PULL")
        before = copy.deepcopy(self.table.items)
        detail = self.service.workout_session(self.identity, cancelled)
        self.assertEqual(detail["executions"][0]["sets"][0]["load_value"], 40)
        self.assertEqual(detail["executions"][0]["sets"][0]["rir"], 2)
        self.assertTrue(detail["executions"][0]["exercise_name"])
        self.assertEqual(self.service.workout_session(self.identity, completed)["executions"][0]["status"], "skipped")
        with self.assertRaises(WorkoutConflict):
            self.service.cancel_workout(self.identity, cancelled, {"expected_revision": 2})
        for sid in [cancelled, "missing"]:
            with self.assertRaises(WorkoutNotFound):
                self.service.workout_session(self.other, sid)
        self.assertEqual(before, self.table.items)

    def test_invalid_limits_and_cursors(self):
        for limit in [0, -1, 51, True, 1.5, "1.0", "no", None, "9" * 5000]:
            with self.subTest(limit=limit), self.assertRaises(InvalidWorkoutInput):
                self.service.list_workout_history(self.identity, limit=limit)
        for raw in ["", "!!!", "a" * 2049, base64.urlsafe_b64encode(json.dumps({"v": 1, "pk": self.identity.pk, "sk": "PROFILE"}).encode()).decode(), "bnVsbA==", "W10="]:
            with self.subTest(cursor=raw), self.assertRaises(InvalidWorkoutInput):
                self.service.list_workout_history(self.identity, cursor=raw)

    def test_backfill_dry_run_and_idempotence_preserves_originals(self):
        sid = self.close()
        for key in list(self.table.items):
            if key[1].startswith(("WORKOUT_HISTORY#", "WORKOUT_SESSION#")):
                del self.table.items[key]
        before = copy.deepcopy(self.table.items)
        self.assertEqual(backfill(self.table, user_id=self.identity.user_id)["missing"], 2)
        self.assertEqual(self.table.items, before)
        self.assertEqual(backfill(self.table, apply=True, user_id=self.identity.user_id)["written"], 2)
        self.assertEqual(backfill(self.table, apply=True, user_id=self.identity.user_id)["written"], 0)
        for key, value in before.items():
            self.assertEqual(value, self.table.items[key])
        self.assertEqual(self.service.workout_session(self.identity, sid)["session"]["status"], "cancelled")

    def test_backfill_active_session_only_creates_locator_and_refuses_conflicts(self):
        payload = self.service.start_workout(self.identity, "PULL")
        locator_key = (self.identity.pk, "WORKOUT_SESSION#" + payload["session"]["session_id"])
        del self.table.items[locator_key]
        before = copy.deepcopy(self.table.items)
        self.assertEqual(backfill(self.table, apply=True, user_id=self.identity.user_id)["written"], 1)
        self.assertEqual(self.service.list_workout_history(self.identity)["sessions"], [])
        for key, item in before.items():
            self.assertEqual(item, self.table.items[key])
        self.table.items[locator_key]["session_sk"] = "conflicting-key"
        before = copy.deepcopy(self.table.items)
        with self.assertRaises(RuntimeError):
            backfill(self.table, apply=True, user_id=self.identity.user_id)
        self.assertEqual(before, self.table.items)

    def test_backfill_retrospective_workout_without_actual_times(self):
        sid = self.close(True)
        for key in list(self.table.items):
            if key[1].startswith(("WORKOUT_HISTORY#", "WORKOUT_SESSION#")):
                del self.table.items[key]
        session = next(item for item in self.table.items.values() if item.get("entity_type") == "workout_session")
        session.update(started_at=None, completed_at=None, actual_time_unknown=True, entry_source="retrospective")
        before = copy.deepcopy(self.table.items)
        self.assertEqual(backfill(self.table, apply=True, user_id=self.identity.user_id)["written"], 2)
        summary = self.service.list_workout_history(self.identity)["sessions"][0]
        self.assertIsNone(summary["started_at"])
        self.assertNotIn("duration_minutes", summary)
        self.assertEqual(summary["actual_local_date"], session["actual_local_date"])
        self.assertTrue(self.service.workout_session(self.identity, sid)["session"]["actual_time_unknown"])
        for key, item in before.items():
            self.assertEqual(item, self.table.items[key])
        self.assertEqual(backfill(self.table, apply=True, user_id=self.identity.user_id)["missing"], 0)

    def test_only_cancelled_history_is_empty_without_mutating_records(self):
        for _ in range(3):
            self.close()
        before = copy.deepcopy(self.table.items)
        self.assertEqual(self.service.list_workout_history(self.identity, limit=1),
                         {"sessions": [], "next_cursor": None})
        self.assertEqual(self.table.items, before)

    def test_cancelled_entries_do_not_shorten_visible_pages(self):
        expected = []
        for _ in range(5):
            expected.insert(0, self.close(True))
            self.close()
        page = self.service.list_workout_history(self.identity, limit=3)
        self.assertEqual([s["session_id"] for s in page["sessions"]], expected[:3])
        rest = self.service.list_workout_history(self.identity, limit=3, cursor=page["next_cursor"])
        self.assertEqual([s["session_id"] for s in rest["sessions"]], expected[3:])
        self.assertIsNone(rest["next_cursor"])

    def test_api_history_and_detail_errors(self):
        sid = self.close(True)
        self.close()
        self.close(True)
        client = TestClient(api.app)
        with patch.object(api, "_service", return_value=self.service), patch.object(api, "_auth_identity", return_value=self.identity):
            page = client.get("/api/workout/history?limit=1")
            self.assertEqual(page.status_code, 200)
            self.assertIn("no-store", page.headers["cache-control"])
            rest = client.get("/api/workout/history", params={"cursor": page.json()["next_cursor"]})
            self.assertEqual(rest.json()["sessions"][0]["session_id"], sid)
            self.assertEqual(client.get(f"/api/workout/sessions/{sid}").status_code, 200)
            for query in ["limit=0", "limit=1.5", "cursor=bad"]:
                self.assertEqual(client.get("/api/workout/history?" + query).status_code, 400)
            self.assertEqual(client.get("/api/workout/sessions/missing").status_code, 404)
        with patch.object(api, "_service", return_value=self.service), patch.object(api, "_auth_identity", return_value=self.other):
            self.assertEqual(client.get(f"/api/workout/sessions/{sid}").status_code, 404)
        with patch.object(api, "_service", return_value=self.service):
            self.assertEqual(client.get("/api/workout/history").status_code, 401)
