import json
import tempfile
import unittest
from pathlib import Path

from macro_bot.catalog_growth import CatalogGrowthService
from macro_bot.models import CatalogSuggestion, CatalogOverlapDecision, LoggedMealRow, MacroTotal
from macro_bot.storage import CatalogSuggestionStore, FoodCatalogStore, MealLogRepository, UserProfileStore


class CatalogGrowthTests(unittest.TestCase):
    def _write_profiles(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "telegram_user_id": 349553317,
                            "username": "Vaanasaurus",
                            "display_name": "Vaan",
                            "daily_target": {
                                "calories": 2200,
                                "protein_g": 160,
                                "carbs_g": 220,
                                "fat_g": 70,
                            },
                            "preferred_cuisines": ["asian"],
                            "preferred_tags": ["meal"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def _write_catalog(self, path: Path) -> None:
        path.write_text(json.dumps({"foods": []}, indent=2), encoding="utf-8")

    def test_refresh_builds_pending_review_suggestions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = MealLogRepository(root / "meals_v2.csv", root / "meals.csv")
            repo.append(
                LoggedMealRow(
                    datetime_iso="2026-04-08T12:00:00",
                    telegram_user_id=349553317,
                    username="vaanasaurus",
                    person="vaan",
                    caption="Gardenia original wrap 1 reduced fat cheese with chicken 270g",
                    calories=540,
                    protein_g=65.0,
                    carbs_g=30.0,
                    fat_g=10.0,
                    confidence=0.8,
                    message_id=1,
                )
            )
            repo.append(
                LoggedMealRow(
                    datetime_iso="2026-04-09T12:00:00",
                    telegram_user_id=349553317,
                    username="vaanasaurus",
                    person="vaan",
                    caption="Gardenia original wrap 1 reduced fat cheese with chicken 280g",
                    calories=674,
                    protein_g=99.5,
                    carbs_g=35.7,
                    fat_g=16.6,
                    confidence=0.82,
                    message_id=2,
                )
            )

            self._write_profiles(root / "user_profiles.json")
            self._write_catalog(root / "food_catalog.json")

            service = CatalogGrowthService(
                meal_log_repository=repo,
                profile_store=UserProfileStore(root / "user_profiles.json"),
                food_catalog_store=FoodCatalogStore(root / "food_catalog.json"),
                suggestion_store=CatalogSuggestionStore(root / "catalog_suggestions.json"),
            )

            stats = service.refresh()
            suggestions = CatalogSuggestionStore(root / "catalog_suggestions.json").list()

            self.assertEqual(stats.suggestion_count, 1)
            self.assertEqual(len(suggestions), 1)
            self.assertEqual(suggestions[0].status, "pending_review")
            self.assertEqual(suggestions[0].occurrence_count, 2)
            self.assertEqual(suggestions[0].telegram_user_id, 349553317)
            self.assertEqual(suggestions[0].eligible_telegram_user_ids, [349553317])
            self.assertAlmostEqual(suggestions[0].macros.calories, 607.0)

    def test_apply_approved_appends_to_catalog_and_marks_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = MealLogRepository(root / "meals_v2.csv", root / "meals.csv")
            self._write_profiles(root / "user_profiles.json")
            self._write_catalog(root / "food_catalog.json")
            suggestion_store = CatalogSuggestionStore(root / "catalog_suggestions.json")
            suggestion_store.save(
                [
                    CatalogSuggestion(
                        suggestion_id="349553317_custom_wrap_12345678",
                        telegram_user_id=349553317,
                        cluster_key="custom wrap chicken",
                        proposed_name="Custom Chicken Wrap",
                        proposed_serving="1 wrap",
                        macros=MacroTotal(610, 52, 34, 16),
                        tags=["meal", "high_protein"],
                        cuisines=[],
                        eligible_telegram_user_ids=[349553317],
                        occurrence_count=3,
                        source_captions=["Custom wrap chicken"],
                        first_seen_iso="2026-04-01T12:00:00",
                        last_seen_iso="2026-04-05T12:00:00",
                        status="approved",
                    )
                ]
            )

            service = CatalogGrowthService(
                meal_log_repository=repo,
                profile_store=UserProfileStore(root / "user_profiles.json"),
                food_catalog_store=FoodCatalogStore(root / "food_catalog.json"),
                suggestion_store=suggestion_store,
            )

            appended = service.apply_approved()
            catalog_entries = FoodCatalogStore(root / "food_catalog.json").list_entries()
            updated_suggestions = suggestion_store.list()

            self.assertEqual(appended, 1)
            self.assertEqual(catalog_entries[0].food_id, "349553317_custom_wrap_12345678")
            self.assertEqual(catalog_entries[0].name, "Custom Chicken Wrap")
            self.assertEqual(updated_suggestions[0].status, "applied")


class _FakeReviewClient:
    async def review_overlaps(self, suggestions, catalog_entries):
        decisions = []
        for item in suggestions:
            if "protein" in item.proposed_name.lower():
                decisions.append(
                    CatalogOverlapDecision(
                        suggestion_id=item.suggestion_id,
                        action="reject_duplicate",
                        duplicate_food_id="double_protein_shake",
                        rationale="Alias duplicate of existing shake.",
                    )
                )
            else:
                decisions.append(
                    CatalogOverlapDecision(
                        suggestion_id=item.suggestion_id,
                        action="keep",
                        rationale="Distinct enough to keep.",
                    )
                )
        return decisions


class CatalogGrowthReviewedTests(unittest.IsolatedAsyncioTestCase):
    def _write_profiles(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "telegram_user_id": 349553317,
                            "username": "Vaanasaurus",
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

    def _write_catalog(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "foods": [
                        {
                            "food_id": "double_protein_shake",
                            "name": "Double Protein Shake",
                            "serving": "2 scoops whey with milk",
                            "macros": {
                                "calories": 380,
                                "protein_g": 48,
                                "carbs_g": 18,
                                "fat_g": 6,
                            },
                            "eligible_telegram_user_ids": [349553317],
                        }
                    ]
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    async def test_refresh_with_review_marks_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = MealLogRepository(root / "meals_v2.csv", root / "meals.csv")
            repo.append(
                LoggedMealRow(
                    datetime_iso="2026-04-08T10:00:00",
                    telegram_user_id=349553317,
                    username="vaanasaurus",
                    person="vaan",
                    caption="ON milk chocolate protein shake. 2 scoops protein and 350ml normal meiji milk",
                    calories=390,
                    protein_g=52.0,
                    carbs_g=21.0,
                    fat_g=6.0,
                    confidence=0.9,
                    message_id=1,
                )
            )
            repo.append(
                LoggedMealRow(
                    datetime_iso="2026-04-09T10:00:00",
                    telegram_user_id=349553317,
                    username="vaanasaurus",
                    person="vaan",
                    caption="ON milk chocolate protein shake. 2 scoops protein and 350ml normal meiji milk",
                    calories=400,
                    protein_g=50.0,
                    carbs_g=20.0,
                    fat_g=5.0,
                    confidence=0.9,
                    message_id=2,
                )
            )
            self._write_profiles(root / "user_profiles.json")
            self._write_catalog(root / "food_catalog.json")

            service = CatalogGrowthService(
                meal_log_repository=repo,
                profile_store=UserProfileStore(root / "user_profiles.json"),
                food_catalog_store=FoodCatalogStore(root / "food_catalog.json"),
                suggestion_store=CatalogSuggestionStore(root / "catalog_suggestions.json"),
                catalog_review_client=_FakeReviewClient(),
            )

            await service.refresh_with_review()
            suggestions = CatalogSuggestionStore(root / "catalog_suggestions.json").list()

            self.assertEqual(len(suggestions), 0)


if __name__ == "__main__":
    unittest.main()
