from typing import Optional

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .catalog_growth import CatalogGrowthService
from .config import BotConfig, load_bot_config
from .handlers import BotHandlers
from .recommendations import RecommendationPlanner
from .services import CatalogReviewClient, MacroEstimatorClient, RecommendationClient
from .storage import CatalogSuggestionStore, FoodCatalogStore, MealLogRepository, UserProfileStore


def build_telegram_application(
    config: Optional[BotConfig] = None,
    *,
    create_updater: bool = True,
) -> Application:
    config = config or load_bot_config()
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

    builder = Application.builder().token(config.bot_token)
    if not create_updater:
        builder = builder.updater(None)
    app = builder.build()
    app.add_handler(CommandHandler("logmeal", handlers.on_logmeal))
    app.add_handler(CommandHandler("openapp", handlers.on_openapp))
    app.add_handler(CommandHandler("suggestmeal", handlers.on_suggestmeal))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_datetime_input))
    app.add_handler(MessageHandler(filters.PHOTO, handlers.on_photo))
    app.add_handler(
        CallbackQueryHandler(
            handlers.on_meal_action,
            pattern=r"^meal:v1:(confirm|smaller|larger|cancel):",
        )
    )
    return app
