import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from macro_bot.catalog_growth import CatalogGrowthService
from macro_bot.services import CatalogReviewClient
from macro_bot.storage import CatalogSuggestionStore, FoodCatalogStore, MealLogRepository, UserProfileStore


def build_service(root: Path, review_client: CatalogReviewClient = None) -> CatalogGrowthService:
    return CatalogGrowthService(
        meal_log_repository=MealLogRepository(
            meals_v2_csv_path=root / "meals_v2.csv",
            legacy_meals_csv_path=root / "meals.csv",
        ),
        profile_store=UserProfileStore(root / "user_profiles.json"),
        food_catalog_store=FoodCatalogStore(root / "food_catalog.json"),
        suggestion_store=CatalogSuggestionStore(root / "catalog_suggestions.json"),
        catalog_review_client=review_client,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh and apply safe food catalog growth suggestions.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh_parser = subparsers.add_parser("refresh", help="Rebuild catalog suggestions from confirmed meals.")
    refresh_parser.add_argument(
        "--telegram-user-id",
        type=int,
        default=0,
        help="Limit refresh to a single Telegram user ID.",
    )
    reviewed_refresh_parser = subparsers.add_parser(
        "refresh-reviewed",
        help="Rebuild catalog suggestions and run model-assisted overlap review.",
    )
    reviewed_refresh_parser.add_argument(
        "--telegram-user-id",
        type=int,
        default=0,
        help="Limit refresh to a single Telegram user ID.",
    )

    apply_parser = subparsers.add_parser("apply-approved", help="Append approved suggestions to food_catalog.json.")
    apply_parser.add_argument(
        "--ids",
        default="",
        help="Comma-separated suggestion IDs to apply. If omitted, applies all suggestions with status=approved.",
    )

    args = parser.parse_args()
    if args.command == "refresh":
        service = build_service(ROOT)
        stats = service.refresh(telegram_user_id=args.telegram_user_id or None)
        print(
            "refreshed suggestions={} telegram_user_ids={}".format(
                stats.suggestion_count,
                ",".join(str(item) for item in stats.refreshed_user_ids) or "none",
            )
        )
        return

    if args.command == "refresh-reviewed":
        service = build_service(
            ROOT,
            review_client=CatalogReviewClient(
                base_url=os.getenv(
                    "MACRO_CATALOG_REVIEW_API",
                    "http://127.0.0.1:9000/catalog/review-overlaps",
                )
            ),
        )
        stats = asyncio.run(
            service.refresh_with_review(telegram_user_id=args.telegram_user_id or None)
        )
        print(
            "refreshed suggestions={} telegram_user_ids={}".format(
                stats.suggestion_count,
                ",".join(str(item) for item in stats.refreshed_user_ids) or "none",
            )
        )
        return

    service = build_service(ROOT)
    ids = [item.strip() for item in args.ids.split(",") if item.strip()]
    appended = service.apply_approved(suggestion_ids=ids or None)
    print(f"applied_entries={appended}")


if __name__ == "__main__":
    main()
