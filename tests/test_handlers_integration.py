import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from macro_bot.config import BotConfig
from macro_bot.handlers import BotHandlers
from macro_bot.models import MacroTotal, MealEstimate, RecommendationResult, RecommendedMeal
from macro_bot.recommendations import PreparedRecommendation


class InMemoryRepo:
    def __init__(self):
        self.rows = []

    def append(self, row):
        self.rows.append(row)


class DummyPlanner:
    def __init__(self):
        self.calls = []

    async def recommend_next_meal(self, telegram_user_id: int, target_date=None):
        self.calls.append((telegram_user_id, target_date))
        prepared = PreparedRecommendation(
            profile=SimpleNamespace(
                to_payload=lambda: {"telegram_user_id": telegram_user_id},
                display_name="User",
            ),
            daily_summary=SimpleNamespace(
                totals=MacroTotal(1200, 80, 120, 35),
            ),
            remaining=SimpleNamespace(
                to_payload=lambda: {},
            ),
            recent_meals=["rice bowl"],
            candidate_foods=[],
        )
        result = RecommendationResult(
            summary="Today so far: 1200 kcal. Remaining: 900 kcal, 60g protein, 110g carbs, 25g fat.",
            today_totals=MacroTotal(1200, 80, 120, 35),
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
        return result, prepared


class HandlersIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        self.config = BotConfig(
            bot_token="token",
            macro_api_url="http://localhost:9000/estimate",
            macro_recommend_api_url="http://localhost:9000/recommend",
            macro_catalog_review_api_url="http://localhost:9000/catalog/review-overlaps",
            mini_app_url="https://example.com/miniapp",
            meals_v2_csv_path=tmp_path / "meals_v2.csv",
            legacy_meals_csv_path=tmp_path / "meals.csv",
            profile_store_path=tmp_path / "profiles.json",
            food_catalog_path=tmp_path / "foods.json",
            catalog_suggestions_path=tmp_path / "catalog_suggestions.json",
        )

    async def asyncTearDown(self):
        self.tmpdir.cleanup()

    async def test_on_photo_to_confirm_writes_row_and_triggers_auto_recommendation(self):
        estimator = AsyncMock()
        estimator.estimate.return_value = MealEstimate(
            meal_name="Meal",
            calories=500,
            protein_g=30,
            carbs_g=60,
            fat_g=10,
            confidence=0.8,
            notes="note",
            total_low=MacroTotal(450, 26, 54, 8),
            total_high=MacroTotal(620, 35, 75, 18),
        )
        repo = InMemoryRepo()
        planner = DummyPlanner()
        handlers = BotHandlers(self.config, estimator, repo, planner)

        upload = AsyncMock()
        upload.download_to_memory = AsyncMock(side_effect=lambda out: out.write(b"img"))
        bot = AsyncMock()
        bot.get_file.return_value = upload

        msg = AsyncMock()
        msg.photo = [SimpleNamespace(file_id="f")]
        msg.caption = "rice"
        msg.chat_id = 10
        msg.message_id = 20
        msg.reply_text.return_value = SimpleNamespace(message_id=30)

        user = SimpleNamespace(id=123, username="vaanasaurus")
        app = SimpleNamespace(bot_data={})
        context = SimpleNamespace(bot=bot, application=app, args=[])
        update = SimpleNamespace(effective_message=msg, effective_user=user)

        await handlers.on_photo(update, context)

        action_token = next(iter(app.bot_data["meal_actions"].keys()))
        query = AsyncMock()
        query.data = f"meal:v1:confirm:{action_token}"
        update_confirm = SimpleNamespace(callback_query=query, effective_user=user)

        with patch("macro_bot.handlers.append_outcome_event"), patch(
            "macro_bot.handlers.append_recommendation_event"
        ), patch("macro_bot.handlers.append_recommendation_skip_event"):
            await handlers.on_meal_action(update_confirm, context)

        self.assertEqual(len(repo.rows), 1)
        self.assertEqual(repo.rows[0].person, "vaan")
        self.assertEqual(planner.calls[0][0], 123)
        bot.send_message.assert_awaited()

        estimator.estimate.assert_awaited()
        kwargs = estimator.estimate.await_args.kwargs
        self.assertIn("persona_hint", kwargs)

    async def test_on_suggestmeal_prompts_for_profile_setup_when_missing(self):
        missing_profile_planner = SimpleNamespace(
            recommend_next_meal=AsyncMock(side_effect=KeyError("missing"))
        )
        handlers = BotHandlers(self.config, AsyncMock(), InMemoryRepo(), missing_profile_planner)
        msg = AsyncMock()
        user = SimpleNamespace(id=999, username="unknownuser")
        app = SimpleNamespace(bot_data={})
        context = SimpleNamespace(bot=AsyncMock(), application=app, args=[])
        update = SimpleNamespace(effective_message=msg, effective_user=user)

        await handlers.on_suggestmeal(update, context)

        args = msg.reply_text.await_args.args
        self.assertIn("Set up your macro targets in the Mini App first.", args[0])
        self.assertIn(self.config.mini_app_url, args[0])

    async def test_on_openapp_sends_setup_link(self):
        handlers = BotHandlers(self.config, AsyncMock(), InMemoryRepo(), DummyPlanner())
        msg = AsyncMock()
        user = SimpleNamespace(id=999, username="unknownuser")
        context = SimpleNamespace(bot=AsyncMock(), application=SimpleNamespace(bot_data={}), args=[])
        update = SimpleNamespace(effective_message=msg, effective_user=user)

        await handlers.on_openapp(update, context)

        args = msg.reply_text.await_args.args
        kwargs = msg.reply_text.await_args.kwargs
        self.assertIn("Set up your macro targets in the Mini App first.", args[0])
        self.assertIsNotNone(kwargs["reply_markup"])


if __name__ == "__main__":
    unittest.main()
