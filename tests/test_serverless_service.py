import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from macro_bot.models import MacroTotal, QuestionnaireAnswers, UserProfile
from macro_bot.serverless_data import DynamoNutritionRepository
from macro_bot.serverless_service import NutritionService, ReadOnlyFoodCatalogStore, InvalidUserInput
from tests.test_serverless_data import _FakeTable, _estimate


class ServerlessServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
        self.table = _FakeTable()
        self.repo = DynamoNutritionRepository(self.table, table_name="fitness", now_fn=lambda: self.now)
        self.service = NutritionService(self.repo, now_fn=lambda: self.now)
        self.answers = QuestionnaireAnswers("male", 30, 180, 80, "moderate", "maintain")

    def _profile(self, identity, calories):
        return UserProfile(
            identity.telegram_user_id,
            identity.username,
            identity.display_name,
            MacroTotal(calories, 150, 200, 60),
            self.answers,
            timezone="Asia/Singapore",
            created_at="2026-01-01T00:00:00Z",
        )

    def test_user_partitions_isolate_profiles_meals_and_recommendation_inputs(self):
        a = self.service.resolve_user(101, "a", "A")
        b = self.service.resolve_user(202, "b", "B")
        self.repo.save_profile(a, self._profile(a, 2000))
        self.repo.save_profile(b, self._profile(b, 3000))
        action = self.service.create_pending_meal(
            a,
            chat_id=1,
            request_message_id=2,
            caption="a meal",
            estimate=_estimate(500),
            eaten_at=datetime(2026, 1, 15, 3, tzinfo=timezone.utc),
        )
        self.service.finalize_action(a, action.token, "confirm")

        self.assertEqual(self.repo.get_profile(a.user_id).daily_target.calories, 2000)
        self.assertEqual(self.repo.get_profile(b.user_id).daily_target.calories, 3000)
        self.assertEqual(self.repo.daily_summary(a, self.now.date(), "Asia/Singapore").meal_count, 1)
        self.assertEqual(self.repo.daily_summary(b, self.now.date(), "Asia/Singapore").meal_count, 0)
        self.assertIsNone(self.service.get_action(b, action.token))

    def test_singapore_day_queries_use_local_midnight_and_catalogue_is_read_only(self):
        identity = self.service.resolve_user(101, "a", "A")
        self.repo.save_profile(identity, self._profile(identity, 2000))
        before = hashlib.sha256(Path("food_catalog.json").read_bytes()).hexdigest()
        at_start = self.service.create_pending_meal(
            identity,
            chat_id=1,
            request_message_id=2,
            caption="local day start",
            estimate=_estimate(400),
            eaten_at=datetime(2026, 1, 14, 16, 0, tzinfo=timezone.utc),
        )
        at_end = self.service.create_pending_meal(
            identity,
            chat_id=1,
            request_message_id=3,
            caption="next local day",
            estimate=_estimate(400),
            eaten_at=datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc),
        )
        self.service.finalize_action(identity, at_start.token, "confirm")
        self.service.finalize_action(identity, at_end.token, "confirm")
        summary = self.repo.daily_summary(identity, datetime(2026, 1, 15).date(), "Asia/Singapore")
        self.assertEqual(summary.meal_count, 1)
        self.assertEqual(summary.meals[0].caption, "local day start")
        ReadOnlyFoodCatalogStore().list_entries()
        after = hashlib.sha256(Path("food_catalog.json").read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_profile_rejects_client_identity_while_preview_and_target_history_remain_safe(self):
        identity = self.service.resolve_user(101, "a", "A")
        payload = {
            "telegram_user_id": 999999,
            "internal_user_id": "someone-else",
            **self.answers.to_payload(),
        }
        with self.assertRaises(InvalidUserInput):
            self.service.save_profile(identity, payload)
        self.assertEqual(len(self.repo.list_targets(identity.user_id)), 0)
        result = self.service.save_profile(identity, self.answers.to_payload())
        self.assertEqual(result["viewer"]["telegram_user_id"], 101)
        self.assertEqual(len(self.repo.list_targets(identity.user_id)), 1)
        preview = NutritionService.preview_payload(payload)
        self.assertEqual(preview["questionnaire_answers"], self.answers.to_payload())

    def test_daily_nutrition_payload_reuses_confirmed_daily_summary_and_local_target_revision(self):
        identity = self.service.resolve_user(101, "a", "A")
        self.repo.save_profile(identity, self._profile(identity, 2000), effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        included = self.service.create_pending_meal(
            identity,
            chat_id=1,
            request_message_id=2,
            caption="breakfast",
            estimate=_estimate(500),
            eaten_at=datetime(2026, 1, 15, 3, tzinfo=timezone.utc),
        )
        excluded = self.service.create_pending_meal(
            identity,
            chat_id=1,
            request_message_id=3,
            caption="yesterday",
            estimate=_estimate(700),
            eaten_at=datetime(2026, 1, 14, 15, 59, tzinfo=timezone.utc),
        )
        self.service.finalize_action(identity, included.token, "confirm")
        self.service.finalize_action(identity, excluded.token, "confirm")

        payload = self.service.daily_nutrition_payload(identity, datetime(2026, 1, 15).date())

        self.assertEqual(payload["date"], "2026-01-15")
        self.assertEqual(payload["today"], "2026-01-15")
        self.assertEqual(payload["timezone"], "Asia/Singapore")
        self.assertEqual(payload["target"]["calories"], 2000.0)
        self.assertEqual(payload["consumed"]["calories"], 500.0)
        self.assertEqual(payload["remaining"]["calories"], 1500.0)
        self.assertEqual(payload["meal_count"], 1)
        self.assertEqual([meal["caption"] for meal in payload["meals"]], ["breakfast"])


if __name__ == "__main__":
    unittest.main()
