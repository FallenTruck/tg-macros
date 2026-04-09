import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx

from macro_bot.models import CatalogSuggestion, LoggedMealRow, MacroTotal, RecommendationRequest, RemainingMacros, UserProfile
from macro_bot.services import CatalogReviewClient, MacroEstimatorClient, MacroEstimatorError, RecommendationClient
from macro_bot.storage import FoodCatalogStore, MealLogRepository, UserProfileStore


class ServicesAndStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_estimator_success(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "meal_name": "meal",
            "calories": 500,
            "protein_g": 30,
            "carbs_g": 60,
            "fat_g": 10,
            "total_best": {"calories": 500, "protein_g": 30, "carbs_g": 60, "fat_g": 10},
            "total_low": {"calories": 450, "protein_g": 26, "carbs_g": 54, "fat_g": 8},
            "total_high": {"calories": 620, "protein_g": 35, "carbs_g": 75, "fat_g": 18},
            "items": [
                {
                    "name": "rice",
                    "portion_g": 200,
                    "assumptions": "1 cup",
                    "calories": 260,
                    "protein_g": 5,
                    "carbs_g": 56,
                    "fat_g": 1,
                }
            ],
            "variance_drivers": ["oil"],
            "confidence": 0.8,
            "notes": "ok",
        }

        client = AsyncMock()
        client.post.return_value = response
        estimator = MacroEstimatorClient("http://localhost:9000/estimate", client=client)

        estimate = await estimator.estimate(b"bytes", "cap", persona_hint="prior meal")
        self.assertEqual(estimate.meal_name, "meal")
        self.assertEqual(int(round(estimate.calories)), 500)

        call_kwargs = client.post.await_args.kwargs
        self.assertEqual(call_kwargs["data"]["persona_hint"], "prior meal")

    async def test_estimator_retries_and_fails(self):
        client = AsyncMock()
        client.post.side_effect = httpx.ConnectError("boom")
        estimator = MacroEstimatorClient(
            "http://localhost:9000/estimate",
            max_retries=1,
            client=client,
        )

        with self.assertRaises(MacroEstimatorError):
            await estimator.estimate(b"bytes", "cap")

    async def test_recommendation_client_success(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "summary": "Today so far: 900 kcal. Remaining: 1100 kcal, 80g protein, 120g carbs, 30g fat.",
            "today_totals": {"calories": 900, "protein_g": 60, "carbs_g": 100, "fat_g": 25},
            "remaining_macros": {"calories": 1100, "protein_g": 80, "carbs_g": 120, "fat_g": 30},
            "suggestions": [
                {
                    "name": "Chicken Rice Bowl",
                    "serving": "1 bowl",
                    "calories": 620,
                    "protein_g": 42,
                    "carbs_g": 68,
                    "fat_g": 18,
                    "fit_rationale": "strong protein fit",
                    "tradeoffs": "fat may run a bit high",
                }
            ],
            "source": "model_ranked",
        }
        client = AsyncMock()
        client.post.return_value = response
        recommender = RecommendationClient("http://localhost:9000/recommend", client=client)
        request = RecommendationRequest(
            telegram_user_id=349553317,
            profile=UserProfile(
                telegram_user_id=349553317,
                username="Vaanasaurus",
                display_name="Vaan",
                daily_target=MacroTotal(2200, 160, 220, 70),
            ),
            today_totals=MacroTotal(900, 60, 100, 25),
            remaining=RemainingMacros.from_target_and_consumed(
                MacroTotal(2200, 160, 220, 70),
                MacroTotal(900, 60, 100, 25),
            ),
            recent_meals=["rice bowl"],
            candidate_foods=[],
        )

        result = await recommender.recommend(request)
        self.assertEqual(result.source, "model_ranked")
        self.assertEqual(result.suggestions[0].name, "Chicken Rice Bowl")

    async def test_catalog_review_client_success(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "decisions": [
                {
                    "suggestion_id": "s1",
                    "action": "reject_duplicate",
                    "duplicate_food_id": "double_protein_shake",
                    "rationale": "Same protein shake pattern.",
                }
            ]
        }
        client = AsyncMock()
        client.post.return_value = response
        reviewer = CatalogReviewClient("http://localhost:9000/catalog/review-overlaps", client=client)
        decisions = await reviewer.review_overlaps(
            suggestions=[
                CatalogSuggestion(
                    suggestion_id="s1",
                    telegram_user_id=349553317,
                    cluster_key="protein shake",
                    proposed_name="ON Protein Shake",
                    proposed_serving="1 logged portion",
                    macros=MacroTotal(390, 50, 20, 5),
                    eligible_telegram_user_ids=[349553317],
                    occurrence_count=3,
                    source_captions=["ON milk chocolate protein shake"],
                    first_seen_iso="2026-04-01T10:00:00",
                    last_seen_iso="2026-04-03T10:00:00",
                )
            ],
            catalog_entries=[],
        )
        self.assertEqual(decisions[0].action, "reject_duplicate")
        self.assertEqual(decisions[0].duplicate_food_id, "double_protein_shake")

    async def test_csv_append_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = MealLogRepository(
                meals_v2_csv_path=tmp_path / "meals_v2.csv",
                legacy_meals_csv_path=tmp_path / "meals.csv",
            )
            row = LoggedMealRow(
                datetime_iso="2026-03-09T12:00:00",
                telegram_user_id=1,
                username="user",
                person="vaan",
                caption="food",
                calories=512,
                protein_g=31.25,
                carbs_g=62.01,
                fat_g=14.49,
                confidence=0.81234,
                message_id=99,
            )
            repo.append(row)

            with (tmp_path / "meals_v2.csv").open("r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["protein_g"], "31.2")
            self.assertEqual(rows[0]["confidence"], "0.812")

    async def test_daily_summary_and_stores(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = MealLogRepository(
                meals_v2_csv_path=tmp_path / "meals_v2.csv",
                legacy_meals_csv_path=tmp_path / "meals.csv",
            )
            repo.append(
                LoggedMealRow(
                    datetime_iso="2026-04-08T08:00:00",
                    telegram_user_id=1,
                    username="vaanasaurus",
                    person="vaan",
                    caption="breakfast",
                    calories=600,
                    protein_g=35.0,
                    carbs_g=60.0,
                    fat_g=18.0,
                    confidence=0.9,
                    message_id=1,
                )
            )
            repo.append(
                LoggedMealRow(
                    datetime_iso="2026-04-08T13:00:00",
                    telegram_user_id=1,
                    username="vaanasaurus",
                    person="vaan",
                    caption="lunch",
                    calories=700,
                    protein_g=45.0,
                    carbs_g=75.0,
                    fat_g=20.0,
                    confidence=0.9,
                    message_id=2,
                )
            )
            repo.append(
                LoggedMealRow(
                    datetime_iso="2026-04-07T13:00:00",
                    telegram_user_id=1,
                    username="vaanasaurus",
                    person="vaan",
                    caption="old lunch",
                    calories=999,
                    protein_g=1.0,
                    carbs_g=1.0,
                    fat_g=1.0,
                    confidence=0.9,
                    message_id=3,
                )
            )

            summary = repo.get_daily_summary(1, date(2026, 4, 8))
            self.assertEqual(summary.meal_count, 2)
            self.assertEqual(summary.totals.calories, 1300)
            self.assertAlmostEqual(summary.totals.protein_g, 80.0)

            profile_path = tmp_path / "profiles.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "profiles": [
                            {
                                "person": "vaan",
                                "display_name": "Vaan",
                                "daily_target": {
                                    "calories": 2200,
                                    "protein_g": 160,
                                    "carbs_g": 220,
                                    "fat_g": 70,
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog_path = tmp_path / "foods.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "foods": [
                            {
                                "food_id": "rice_bowl",
                                "name": "Rice Bowl",
                                "serving": "1 bowl",
                                "macros": {
                                    "calories": 600,
                                    "protein_g": 35,
                                    "carbs_g": 70,
                                    "fat_g": 18,
                                },
                                "people": ["vaan"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            profile_store = UserProfileStore(profile_path)
            catalog_store = FoodCatalogStore(catalog_path)
            self.assertEqual(profile_store.get(349553317).display_name, "Vaan")
            self.assertEqual(catalog_store.list_entries()[0].name, "Rice Bowl")
            self.assertEqual(
                catalog_store.list_entries()[0].eligible_telegram_user_ids,
                [349553317],
            )


if __name__ == "__main__":
    unittest.main()
