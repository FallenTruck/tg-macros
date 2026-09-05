import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from macro_bot.direct_estimator import extract_caption_facts
from macro_bot.formatting import build_adjustment_keyboard, format_macro_message
from macro_bot.models import MealEstimate, MealItemEstimate, MacroTotal, PendingMealAction, QuestionnaireAnswers, UserProfile
from macro_bot.serverless_data import DynamoNutritionRepository
from tests.test_serverless_data import _FakeTable


def _estimate() -> MealEstimate:
    items = [
        MealItemEstimate("Chicken", 160, "skin unclear", 300, 35, 0, 16, 100, 220, 0.8, 0.5, "partially_occluded"),
        MealItemEstimate("Rice", 200, "cooked rice", 260, 5, 56, 1, 120, 280, 0.9, 0.5, "partially_occluded"),
        MealItemEstimate("Sauce/oil", 20, "amount hidden", 90, 0, 2, 9, 5, 40, 0.5, 0.3, "uncertain"),
    ]
    return MealEstimate(
        "Chicken rice", 650, 40, 58, 26, 0.7, "", items,
        MacroTotal(560, 32, 48, 18), MacroTotal(820, 52, 78, 38),
        ["sauce/oil quantity"],
    )


class NutritionPhase2Tests(unittest.TestCase):
    def test_caption_facts_are_explicit_and_high_signal(self):
        facts = extract_caption_facts("200g chicken, 2 eggs, skin removed, half rice, little oil")
        self.assertIn("skin_removed", facts)
        self.assertIn("half_rice", facts)
        self.assertIn("chicken=200g", facts)
        self.assertIn("eggs=2", facts)

    def test_targeted_correction_changes_matching_component_and_not_original(self):
        estimate = _estimate()
        corrected = estimate.adjust_category("skin", "removed")
        self.assertLess(corrected.fat_g, estimate.fat_g)
        self.assertEqual(estimate.fat_g, 26)
        self.assertEqual(corrected.items[1].name, "Rice")

    def test_adjustment_keyboard_is_context_aware(self):
        action = PendingMealAction("token", 1, 2, 3, "u", "rice", _estimate())
        markup = build_adjustment_keyboard(action)
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertTrue(any(":fix:base:" in callback for callback in callbacks))
        self.assertTrue(any(":fix:skin:removed:" in callback for callback in callbacks))
        self.assertTrue(any(":fix:sauce:" in callback for callback in callbacks))

    def test_serverless_correction_is_user_scoped_and_preserves_original(self):
        table = _FakeTable()
        repo = DynamoNutritionRepository(table, table_name="fitness", now_fn=lambda: datetime(2026, 1, 15, tzinfo=timezone.utc))
        owner = repo.resolve_identity(101, "owner", "Owner")
        other = repo.resolve_identity(202, "other", "Other")
        action = repo.create_pending_meal(owner, chat_id=1, request_message_id=2, caption="chicken rice", estimate=_estimate())
        corrected = repo.apply_correction(owner, action.token, "skin", "removed")
        self.assertLess(corrected.estimate.fat_g, action.estimate.fat_g)
        self.assertEqual(corrected.original_estimate.fat_g, action.estimate.fat_g)
        self.assertIsNone(repo.get_action(other, action.token))
        records = repo.list_corrections(owner)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["final_status"], "pending")
        repo.finalize_action(owner, action.token, "confirm")
        self.assertEqual(repo.list_corrections(owner)[0]["final_status"], "confirmed")

    def test_prior_requires_two_confirmed_corrections_and_is_user_specific(self):
        table = _FakeTable()
        now = lambda: datetime(2026, 1, 15, tzinfo=timezone.utc)
        repo = DynamoNutritionRepository(table, table_name="fitness", now_fn=now)
        owner = repo.resolve_identity(101, "owner", "Owner")
        other = repo.resolve_identity(202, "other", "Other")
        for index in range(2):
            action = repo.create_pending_meal(owner, chat_id=1, request_message_id=index + 1, caption=f"meal {index}", estimate=_estimate())
            repo.apply_correction(owner, action.token, "skin", "removed")
            repo.finalize_action(owner, action.token, "confirm")
        self.assertIn("skin is usually removed", repo.persona_hint(owner, "new meal"))
        self.assertEqual(repo.persona_hint(other, "new meal"), "")

    def test_formatter_surfaces_items_and_stays_compact(self):
        action = PendingMealAction("token", 1, 2, 3, "u", "", _estimate())
        message = format_macro_message(action.estimate)
        self.assertIn("Items:", message)
        self.assertIn("Chicken", message)
        self.assertIn("Main uncertainty", message)
        self.assertLess(len(message), 4096)


if __name__ == "__main__":
    unittest.main()
