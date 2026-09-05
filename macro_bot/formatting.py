from datetime import datetime
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from .models import MealEstimate, PendingMealAction, RecommendationResult


def parse_meal_datetime(raw_text: str, fmt: str) -> datetime:
    return datetime.strptime(raw_text.strip(), fmt)


def build_meal_keyboard(token: str) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("✅ Looks right", callback_data=f"meal:v1:confirm:{token}")],
        [InlineKeyboardButton("✏️ Adjust", callback_data=f"meal:v1:adjust:{token}")],
        [
            InlineKeyboardButton("⬇️ Smaller", callback_data=f"meal:v1:smaller:{token}"),
            InlineKeyboardButton("⬆️ Larger", callback_data=f"meal:v1:larger:{token}"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"meal:v1:cancel:{token}")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_adjustment_keyboard(action: PendingMealAction) -> InlineKeyboardMarkup:
    """Expose only corrections relevant to the current estimate."""

    names = " ".join(item.name.lower() for item in action.estimate.items)
    rows = []
    if any(term in names for term in ("rice", "noodle", "pasta", "bread", "roti", "chapati", "grain")):
        rows.append([
            InlineKeyboardButton("Base: half", callback_data=f"meal:v1:fix:base:half:{action.token}"),
            InlineKeyboardButton("Base: less", callback_data=f"meal:v1:fix:base:less:{action.token}"),
        ])
    if any(term in names for term in ("chicken", "duck")):
        rows.append([InlineKeyboardButton("Chicken skin removed", callback_data=f"meal:v1:fix:skin:removed:{action.token}")])
    if any(term in names for term in ("sauce", "gravy", "dressing", "oil", "curry")):
        rows.append([
            InlineKeyboardButton("Sauce/oil: light", callback_data=f"meal:v1:fix:sauce:light:{action.token}"),
            InlineKeyboardButton("Sauce/oil: heavy", callback_data=f"meal:v1:fix:sauce:heavy:{action.token}"),
        ])
    rows.append([
        InlineKeyboardButton("Whole portion smaller", callback_data=f"meal:v1:fix:portion:smaller:{action.token}"),
        InlineKeyboardButton("Whole portion larger", callback_data=f"meal:v1:fix:portion:larger:{action.token}"),
    ])
    rows.append([InlineKeyboardButton("↩️ Back", callback_data=f"meal:v1:back:{action.token}")])
    return InlineKeyboardMarkup(rows)


def build_setup_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Open Nutrition Workspace", web_app=WebAppInfo(url=url))]]
    )


def build_direct_setup_keyboard(url: str) -> InlineKeyboardMarkup:
    """Build the group-safe button for a Telegram Mini App deep link."""

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Open Nutrition Workspace", url=url)]]
    )


def build_mini_app_direct_link(bot_username: str, launch_token: str) -> str:
    """Return a Main Mini App deep link carrying only an opaque token."""

    username = str(bot_username or "").strip().lstrip("@")
    token = str(launch_token or "").strip()
    if not username or not token:
        raise ValueError("bot username and launch token are required")
    return f"https://t.me/{quote(username, safe='')}?startapp={quote(token, safe='')}"


def format_macro_message(estimate: MealEstimate) -> str:
    if estimate.total_low and estimate.total_high:
        range_line = (
            f"- Range: {int(round(estimate.total_low.calories))}"
            f"–{int(round(estimate.total_high.calories))} kcal"
        )
    else:
        range_line = "- Range: not available"

    follow_up = (
        f"- Quick check: {estimate.follow_up_question}\n"
        if estimate.follow_up_question
        else ""
    )
    uncertainty = (
        f"- Main uncertainty: {'; '.join(str(item) for item in estimate.variance_drivers[:2])}\n"
        if estimate.variance_drivers
        else ""
    )
    item_lines = []
    for item in estimate.items[:4]:
        item_line = f"• {item.name[:60]}: ~{int(round(item.portion_g))}g · {int(round(item.calories))} kcal"
        if item.protein_g >= 10:
            item_line += f" · {item.protein_g:.0f}g protein"
        item_lines.append(item_line)
    if len(estimate.items) > 4:
        item_lines.append(f"• +{len(estimate.items) - 4} smaller component(s)")
    assumptions = estimate.assumptions_summary()
    assumption_details = estimate.assumptions_detail(max_lines=3)
    message = (
        "🍽️ Macro estimate\n"
        f"- Meal: {estimate.meal_name[:160]}\n"
        f"- Calories: {int(round(float(estimate.calories)))} kcal\n"
        f"- Protein: {float(estimate.protein_g):.1f} g\n"
        f"- Carbs: {float(estimate.carbs_g):.1f} g\n"
        f"- Fat: {float(estimate.fat_g):.1f} g\n"
        f"{range_line}\n"
        + ("Items:\n" + "\n".join(item_lines) + "\n" if item_lines else "")
        + f"- Assumptions: {assumptions}\n"
        + ("What I'm assuming:\n" + "\n".join(f"• {line}" for line in assumption_details) + "\n" if assumption_details else "")
        + f"{uncertainty}"
        + f"- Confidence: {int(float(estimate.confidence) * 100)}%\n"
        + f"{follow_up}"
        + "Choose ✅ Looks right, or ✏️ Adjust if one assumption is off."
    )
    return message if len(message) <= 4000 else message[:3990].rstrip() + "…"


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

    message = "\n".join(lines)
    return message if len(message) <= 4000 else message[:3990].rstrip() + "…"


def format_profile_setup_message(setup_url: str) -> str:
    return (
        "Open the Mini App in Telegram first.\n"
        "Open it in Telegram using the button below, or this link:\n"
        f"{setup_url}"
    )


def format_workout_setup_message(setup_url: str) -> str:
    return (
        "Open the Workout Mini App in Telegram.\n"
        "Your workout details stay private to your account:\n"
        f"{setup_url}"
    )
