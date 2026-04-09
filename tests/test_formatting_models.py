import unittest

from macro_bot.formatting import (
    format_macro_message,
    format_recommendation_message,
    parse_meal_datetime,
)
from macro_bot.models import (
    MealEstimate,
    MacroTotal,
    PendingMealAction,
    RecommendedMeal,
    RecommendationResult,
)


class FormattingAndModelTests(unittest.TestCase):
    def test_parse_meal_datetime_success(self):
        parsed = parse_meal_datetime("09-03-2026 14:30", "%d-%m-%Y %H:%M")
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.month, 3)
        self.assertEqual(parsed.day, 9)
        self.assertEqual(parsed.hour, 14)
        self.assertEqual(parsed.minute, 30)

    def test_parse_meal_datetime_invalid(self):
        with self.assertRaises(ValueError):
            parse_meal_datetime("2026-03-09 14:30", "%d-%m-%Y %H:%M")

    def test_scale_logic_and_formatting(self):
        action = PendingMealAction(
            token="abc",
            chat_id=1,
            request_message_id=2,
            telegram_user_id=3,
            username="user",
            caption="caption",
            estimate=MealEstimate(
                meal_name="Rice bowl",
                calories=500,
                protein_g=30,
                carbs_g=60,
                fat_g=10,
                confidence=0.8,
                notes="note",
                total_low=MacroTotal(440, 24, 54, 8),
                total_high=MacroTotal(620, 38, 72, 16),
            ),
        )

        action.scale(0.8)
        self.assertEqual(int(round(action.estimate.calories)), 400)
        self.assertAlmostEqual(action.estimate.protein_g, 24.0)

        action.scale(1.2)
        self.assertEqual(int(round(action.estimate.calories)), 480)
        self.assertAlmostEqual(action.estimate.protein_g, 28.8)

        message = format_macro_message(action.estimate)
        self.assertIn("Calories: 480 kcal", message)
        self.assertIn("Protein: 28.8 g", message)
        self.assertIn("Range:", message)
        self.assertIn("Assumptions:", message)

    def test_recommendation_formatting(self):
        result = RecommendationResult(
            summary="Today so far: 1200 kcal. Remaining: 900 kcal, 60g protein, 110g carbs, 25g fat.",
            today_totals=MacroTotal(1200, 90, 110, 35),
            remaining_macros=MacroTotal(900, 60, 110, 25),
            suggestions=[
                RecommendedMeal(
                    name="Grilled Chicken Wrap",
                    serving="1 wrap",
                    calories=520,
                    protein_g=42,
                    carbs_g=46,
                    fat_g=17,
                    fit_rationale="strong protein fit",
                    tradeoffs="fat may run a bit high",
                )
            ],
            source="model_ranked",
        )

        message = format_recommendation_message(result)
        self.assertIn("Next meal suggestions", message)
        self.assertIn("Grilled Chicken Wrap", message)
        self.assertIn("Why:", message)
        self.assertIn("Watch:", message)

    def test_recommendation_skip_formatting(self):
        result = RecommendationResult(
            summary="You have less than 200 kcal remaining for today.",
            today_totals=MacroTotal(2100, 150, 220, 68),
            remaining_macros=MacroTotal(80, 10, 0, 2),
            suggestions=[],
            source="skipped",
        )

        message = format_recommendation_message(result)
        self.assertIn("Recommendation check", message)
        self.assertIn("less than 200 kcal", message)


if __name__ == "__main__":
    unittest.main()
