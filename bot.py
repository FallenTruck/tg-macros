import logging
import warnings

warnings.filterwarnings(
    "ignore",
    message="urllib3 v2 only supports OpenSSL 1.1.1+.*",
)

import urllib3
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from macro_bot.catalog_growth import CatalogGrowthService
from macro_bot.config import load_bot_config
from macro_bot.handlers import BotHandlers
from macro_bot.recommendations import RecommendationPlanner
from macro_bot.services import CatalogReviewClient, MacroEstimatorClient, RecommendationClient
from macro_bot.storage import CatalogSuggestionStore, FoodCatalogStore, MealLogRepository, UserProfileStore

urllib3.disable_warnings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def main() -> None:
    config = load_bot_config()
    estimator_client = MacroEstimatorClient(base_url=config.macro_api_url)
    recommendation_client = RecommendationClient(base_url=config.macro_recommend_api_url)
    catalog_review_client = CatalogReviewClient(base_url=config.macro_catalog_review_api_url)
    meal_log_repository = MealLogRepository(
        meals_v2_csv_path=config.meals_v2_csv_path,
        legacy_meals_csv_path=config.legacy_meals_csv_path,
    )
    profile_store = UserProfileStore(profile_path=config.profile_store_path)
    food_catalog_store = FoodCatalogStore(catalog_path=config.food_catalog_path)
    suggestion_store = CatalogSuggestionStore(suggestion_path=config.catalog_suggestions_path)
    recommendation_planner = RecommendationPlanner(
        meal_log_repository=meal_log_repository,
        profile_store=profile_store,
        food_catalog_store=food_catalog_store,
        recommendation_client=recommendation_client,
    )
    catalog_growth_service = CatalogGrowthService(
        meal_log_repository=meal_log_repository,
        profile_store=profile_store,
        food_catalog_store=food_catalog_store,
        suggestion_store=suggestion_store,
        catalog_review_client=catalog_review_client,
    )
    handlers = BotHandlers(
        config=config,
        estimator_client=estimator_client,
        meal_log_repository=meal_log_repository,
        recommendation_planner=recommendation_planner,
        catalog_growth_service=catalog_growth_service,
    )

    app = Application.builder().token(config.bot_token).build()
    app.add_handler(CommandHandler("logmeal", handlers.on_logmeal))
    app.add_handler(CommandHandler("setme", handlers.on_setme))
    app.add_handler(CommandHandler("suggestmeal", handlers.on_suggestmeal))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_datetime_input))
    app.add_handler(MessageHandler(filters.PHOTO, handlers.on_photo))
    app.add_handler(
        CallbackQueryHandler(
            handlers.on_meal_action,
            pattern=r"^meal:v1:(confirm|smaller|larger|cancel):",
        )
    )
    app.run_polling()


if __name__ == "__main__":
    main()
