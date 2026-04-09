import unittest
from datetime import datetime, timedelta

from macro_bot.models import MealEstimate, MacroTotal, PendingMealAction
from macro_bot.state import MealWorkflowStore


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.bot_data = {}
        self.store = MealWorkflowStore(
            bot_data=self.bot_data,
            action_ttl_seconds=3600,
            datetime_ttl_seconds=1800,
        )

    def test_action_transitions(self):
        action = PendingMealAction(
            token="tok",
            chat_id=1,
            request_message_id=2,
            telegram_user_id=100,
            username="u",
            caption="c",
            estimate=MealEstimate("meal", 100, 10, 10, 2, 0.8, "n"),
        )
        self.store.add_action(action)
        self.assertEqual(self.store.get_action("tok").status, "pending")

        action.confirm()
        self.assertEqual(action.status, "confirmed")

        with self.assertRaises(ValueError):
            action.cancel()

    def test_token_expiry_cleanup(self):
        action = PendingMealAction(
            token="old",
            chat_id=1,
            request_message_id=2,
            telegram_user_id=100,
            username="u",
            caption="c",
            estimate=MealEstimate("meal", 100, 10, 10, 2, 0.8, "n"),
        )
        action.created_at = datetime.utcnow() - timedelta(hours=2)
        self.store.add_action(action)

        self.store.cleanup()
        self.assertIsNone(self.store.get_action("old"))

    def test_pending_datetime_expiry_cleanup(self):
        self.store.mark_awaiting_datetime(42)
        self.bot_data["pending_meal_dt"][42] = {
            "datetime": "2026-03-09T12:00:00",
            "created_at": (datetime.utcnow() - timedelta(hours=2)).isoformat(timespec="seconds"),
        }

        self.store.cleanup()
        self.assertFalse(self.store.is_awaiting_datetime(42))
        self.assertIsNone(self.store.pop_pending_datetime(42))

    def test_persona_hint_for_similar_caption(self):
        estimate = MealEstimate(
            meal_name="fried rice",
            calories=600,
            protein_g=20,
            carbs_g=70,
            fat_g=20,
            confidence=0.8,
            notes="test",
            total_low=MacroTotal(540, 18, 63, 16),
            total_high=MacroTotal(740, 24, 84, 28),
        )

        self.store.record_confirmed_meal(7, "Garlic fried rice and egg", estimate)
        hint = self.store.get_persona_hint(7, "garlic   fried rice and egg")
        self.assertIn("Similar prior meal detected", hint)


if __name__ == "__main__":
    unittest.main()
