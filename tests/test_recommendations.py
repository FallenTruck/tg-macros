import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from macro_bot.models import LoggedMealRow
from macro_bot.recommendations import RecommendationPlanner
from macro_bot.services import RecommendationError
from macro_bot.storage import FoodCatalogStore, MealLogRepository, UserProfileStore


class _FailingRecommendationClient:
    async def recommend(self, request):
        raise RecommendationError("boom")


class _EchoRecommendationClient:
    def __init__(self):
        self.last_request = None

    async def recommend(self, request):
        self.last_request = request
        top = request.candidate_foods[0]
        from macro_bot.models import RecommendedMeal, RecommendationResult

        return RecommendationResult(
            summary="Model-ranked suggestion.",
            today_totals=request.today_totals,
            remaining_macros=request.remaining.remaining,
            suggestions=[
                RecommendedMeal.from_candidate(
                    top,
                    fit_rationale="best fit",
                    tradeoffs="none",
                )
            ],
            source="model_ranked",
        )


class RecommendationPlannerTests(unittest.IsolatedAsyncioTestCase):
    def _write_profiles_and_catalog(self, tmp_path: Path):
        (tmp_path / "profiles.json").write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "telegram_user_id": 559404539,
                            "username": "poojyyy20",
                            "display_name": "Pooja",
                            "daily_target": {
                                "calories": 1800,
                                "protein_g": 90,
                                "carbs_g": 220,
                                "fat_g": 55,
                            },
                            "restrictions": ["vegetarian"],
                            "preferred_cuisines": ["indian"],
                            "preferred_tags": ["meal"],
                            "preferred_staples": ["paneer", "dosa"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "foods.json").write_text(
            json.dumps(
                {
                    "foods": [
                        {
                            "food_id": "paneer_wrap",
                            "name": "Paneer Wrap",
                            "serving": "1 wrap",
                            "macros": {
                                "calories": 520,
                                "protein_g": 26,
                                "carbs_g": 48,
                                "fat_g": 18,
                            },
                            "tags": ["vegetarian", "meal"],
                            "cuisines": ["indian"],
                            "eligible_telegram_user_ids": [559404539],
                        },
                        {
                            "food_id": "dosa_set",
                            "name": "Dosa Sambar Set",
                            "serving": "2 dosa + sambar",
                            "macros": {
                                "calories": 500,
                                "protein_g": 16,
                                "carbs_g": 70,
                                "fat_g": 14,
                            },
                            "tags": ["vegetarian", "meal"],
                            "cuisines": ["indian"],
                            "eligible_telegram_user_ids": [559404539],
                        },
                        {
                            "food_id": "chicken_wrap",
                            "name": "Chicken Wrap",
                            "serving": "1 wrap",
                            "macros": {
                                "calories": 540,
                                "protein_g": 38,
                                "carbs_g": 44,
                                "fat_g": 18,
                            },
                            "tags": ["meal"],
                            "cuisines": ["western"],
                            "eligible_telegram_user_ids": [349553317],
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

    def _make_repo(self, tmp_path: Path) -> MealLogRepository:
        return MealLogRepository(
            meals_v2_csv_path=tmp_path / "meals_v2.csv",
            legacy_meals_csv_path=tmp_path / "meals.csv",
        )

    async def test_prepare_filters_restrictions_and_penalizes_repeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_profiles_and_catalog(tmp_path)
            repo = self._make_repo(tmp_path)
            repo.append(
                LoggedMealRow(
                    datetime_iso="2026-04-07T19:00:00",
                    telegram_user_id=559404539,
                    username="poojyyy20",
                    person="pooja",
                    caption="Paneer Wrap",
                    calories=520,
                    protein_g=26.0,
                    carbs_g=48.0,
                    fat_g=18.0,
                    confidence=0.8,
                    message_id=10,
                )
            )

            planner = RecommendationPlanner(
                meal_log_repository=repo,
                profile_store=UserProfileStore(tmp_path / "profiles.json"),
                food_catalog_store=FoodCatalogStore(tmp_path / "foods.json"),
                recommendation_client=_EchoRecommendationClient(),
            )

            prepared = planner.prepare(559404539, target_date=date(2026, 4, 8))
            candidate_names = [item.name for item in prepared.candidate_foods]

            self.assertNotIn("Chicken Wrap", candidate_names)
            self.assertIn("Paneer Wrap", candidate_names)
            self.assertIn("Dosa Sambar Set", candidate_names)
            self.assertLess(
                next(item.fit_score for item in prepared.candidate_foods if item.name == "Paneer Wrap"),
                next(item.fit_score for item in prepared.candidate_foods if item.name == "Dosa Sambar Set"),
            )

    async def test_recommend_next_meal_falls_back_when_client_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_profiles_and_catalog(tmp_path)
            repo = self._make_repo(tmp_path)
            planner = RecommendationPlanner(
                meal_log_repository=repo,
                profile_store=UserProfileStore(tmp_path / "profiles.json"),
                food_catalog_store=FoodCatalogStore(tmp_path / "foods.json"),
                recommendation_client=_FailingRecommendationClient(),
            )

            result, prepared = await planner.recommend_next_meal(559404539, target_date=date(2026, 4, 8))

            self.assertEqual(result.source, "deterministic_fallback")
            self.assertTrue(result.suggestions)
            self.assertFalse(prepared.skip_reason)

    async def test_recommend_next_meal_skips_when_day_is_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_profiles_and_catalog(tmp_path)
            repo = self._make_repo(tmp_path)
            repo.append(
                LoggedMealRow(
                    datetime_iso="2026-04-08T20:00:00",
                    telegram_user_id=559404539,
                    username="poojyyy20",
                    person="pooja",
                    caption="heavy dinner",
                    calories=1900,
                    protein_g=95.0,
                    carbs_g=230.0,
                    fat_g=60.0,
                    confidence=0.8,
                    message_id=11,
                )
            )
            planner = RecommendationPlanner(
                meal_log_repository=repo,
                profile_store=UserProfileStore(tmp_path / "profiles.json"),
                food_catalog_store=FoodCatalogStore(tmp_path / "foods.json"),
                recommendation_client=_EchoRecommendationClient(),
            )

            result, prepared = await planner.recommend_next_meal(559404539, target_date=date(2026, 4, 8))

            self.assertEqual(result.source, "skipped")
            self.assertFalse(result.suggestions)
            self.assertTrue(prepared.skip_reason)


if __name__ == "__main__":
    unittest.main()
