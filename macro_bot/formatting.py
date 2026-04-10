from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .models import MealEstimate, PendingMealAction, RecommendationResult


def parse_meal_datetime(raw_text: str, fmt: str) -> datetime:
    return datetime.strptime(raw_text.strip(), fmt)


def build_meal_keyboard(token: str) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("✅ Log", callback_data=f"meal:v1:confirm:{token}")],
        [
            InlineKeyboardButton("⬇️ -20%", callback_data=f"meal:v1:smaller:{token}"),
            InlineKeyboardButton("⬆️ +20%", callback_data=f"meal:v1:larger:{token}"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"meal:v1:cancel:{token}")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_setup_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Open Macro Setup", url=url)]]
    )


def format_macro_message(estimate: MealEstimate) -> str:
    if estimate.total_low and estimate.total_high:
        range_line = (
            f"- Range: {int(round(estimate.total_low.calories))}"
            f"–{int(round(estimate.total_high.calories))} kcal"
        )
    else:
        range_line = "- Range: not available"

    return (
        "🍽️ Macro estimate\n"
        f"- Meal: {estimate.meal_name}\n"
        f"- Calories: {int(round(float(estimate.calories)))} kcal\n"
        f"- Protein: {float(estimate.protein_g):.1f} g\n"
        f"- Carbs: {float(estimate.carbs_g):.1f} g\n"
        f"- Fat: {float(estimate.fat_g):.1f} g\n"
        f"{range_line}\n"
        f"- Assumptions: {estimate.assumptions_summary()}\n"
        f"- Confidence: {int(float(estimate.confidence) * 100)}%\n"
        "Controls: ⬇️ -20% / ⬆️ +20% then ✅ Log"
    )


def format_pending_message(action: PendingMealAction) -> str:
    return format_macro_message(action.estimate)


def _format_macro_total_inline(calories: float, protein_g: float, carbs_g: float, fat_g: float) -> str:
    return (
        f"{int(round(calories))} kcal"
        f" | P {protein_g:.1f}g"
        f" | C {carbs_g:.1f}g"
        f" | F {fat_g:.1f}g"
    )


def format_recommendation_message(result: RecommendationResult) -> str:
    if not result.suggestions:
        return (
            "✅ Recommendation check\n"
            f"- Status: {result.summary}\n"
            f"- Today: {_format_macro_total_inline(**result.today_totals.to_payload())}\n"
            f"- Remaining: {_format_macro_total_inline(**result.remaining_macros.to_payload())}"
        )

    lines = [
        "🥗 Next meal suggestions",
        f"- Summary: {result.summary}",
        (
            "- Today: "
            f"{_format_macro_total_inline(**result.today_totals.to_payload())}"
        ),
        (
            "- Remaining: "
            f"{_format_macro_total_inline(**result.remaining_macros.to_payload())}"
        ),
    ]

    for index, suggestion in enumerate(result.suggestions, start=1):
        lines.extend(
            [
                f"{index}. {suggestion.name} ({suggestion.serving})",
                "   "
                + _format_macro_total_inline(
                    suggestion.calories,
                    suggestion.protein_g,
                    suggestion.carbs_g,
                    suggestion.fat_g,
                ),
                f"   Why: {suggestion.fit_rationale}",
                f"   Watch: {suggestion.tradeoffs}",
            ]
        )

    return "\n".join(lines)


def format_profile_setup_message(setup_url: str) -> str:
    return (
        "Set up your macro targets in the Mini App first.\n"
        "Open it in Telegram using the button below, or this link:\n"
        f"{setup_url}"
    )
