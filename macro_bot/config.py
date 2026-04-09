import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class BotConfig:
    bot_token: str
    macro_api_url: str
    macro_recommend_api_url: str
    macro_catalog_review_api_url: str
    mini_app_url: str
    meals_v2_csv_path: Path
    legacy_meals_csv_path: Path
    profile_store_path: Path
    food_catalog_path: Path
    catalog_suggestions_path: Path
    date_input_format: str = "%d-%m-%Y %H:%M"
    pending_action_ttl_seconds: int = 3600
    pending_datetime_ttl_seconds: int = 1800

CSV_FIELDNAMES = [
    "datetime",
    "telegram_user_id",
    "username",
    "person",
    "caption",
    "calories",
    "protein_g",
    "carbs_g",
    "fat_g",
    "confidence",
    "message_id",
]


def load_bot_config() -> BotConfig:
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN not set")

    root = Path(__file__).resolve().parent.parent
    service_port = os.getenv("PORT", "9000")
    local_api_base_url = os.getenv("LOCAL_API_BASE_URL", f"http://127.0.0.1:{service_port}")
    return BotConfig(
        bot_token=bot_token,
        macro_api_url=os.getenv("MACRO_API", f"{local_api_base_url}/estimate"),
        macro_recommend_api_url=os.getenv("MACRO_RECOMMEND_API", f"{local_api_base_url}/recommend"),
        macro_catalog_review_api_url=os.getenv(
            "MACRO_CATALOG_REVIEW_API",
            f"{local_api_base_url}/catalog/review-overlaps",
        ),
        mini_app_url=os.getenv("MINI_APP_URL", "https://example.com/miniapp"),
        meals_v2_csv_path=root / "meals_v2.csv",
        legacy_meals_csv_path=root / "meals.csv",
        profile_store_path=root / "user_profiles.json",
        food_catalog_path=root / "food_catalog.json",
        catalog_suggestions_path=root / "catalog_suggestions.json",
    )
