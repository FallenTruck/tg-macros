import base64
import hashlib
import hmac
import io
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Body, FastAPI, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from PIL import Image
from telegram import Update

from macro_bot.models import QUESTIONNAIRE_VERSION, QuestionnaireAnswers, UserProfile
from macro_bot.profile_targets import (
    ACTIVITY_LEVEL_OPTIONS,
    GOAL_OPTIONS,
    derive_daily_target,
    questionnaire_meta_payload,
)
from macro_bot.storage import UserProfileStore
from macro_bot.telegram_app import build_telegram_application

load_dotenv()

logger = logging.getLogger("macro_api")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
TELEGRAM_WEBHOOK_BASE_URL = os.getenv("TELEGRAM_WEBHOOK_BASE_URL", "").strip().rstrip("/")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
TELEGRAM_WEBHOOK_PATH = os.getenv("TELEGRAM_WEBHOOK_PATH", "/telegram/webhook")
MODEL = "gpt-5.4"
RECOMMEND_MODEL = os.getenv("OPENAI_RECOMMEND_MODEL", "gpt-4.1-mini")
CATALOG_REVIEW_MODEL = os.getenv("OPENAI_CATALOG_REVIEW_MODEL", "gpt-4.1-mini")
VISION_DETAIL = "high"
VISION_MAX_SIDE = 768
MAX_UPLOAD_BYTES = 6_000_000
CALORIE_CONSISTENCY_TOLERANCE_KCAL = 120
CALORIE_CONSISTENCY_TOLERANCE_RATIO = 0.20
ROOT_DIR = Path(__file__).resolve().parent
METRICS_DIR = ROOT_DIR / "metrics"
ESTIMATES_LOG_PATH = METRICS_DIR / "estimates.jsonl"
ALLOWED_VISION_DETAILS = {"low", "high"}
TELEGRAM_INIT_DATA_MAX_AGE_SECONDS = int(os.getenv("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "3600"))
MINIAPP_DIR = ROOT_DIR / "miniapp"
USER_PROFILES_PATH = ROOT_DIR / "user_profiles.json"

MACRO_TOTAL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "calories": {"type": "number"},
        "protein_g": {"type": "number"},
        "carbs_g": {"type": "number"},
        "fat_g": {"type": "number"},
    },
    "required": ["calories", "protein_g", "carbs_g", "fat_g"],
}

MEAL_ITEM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "portion_g": {"type": "number"},
        "assumptions": {"type": "string"},
        "calories": {"type": "number"},
        "protein_g": {"type": "number"},
        "carbs_g": {"type": "number"},
        "fat_g": {"type": "number"},
    },
    "required": ["name", "portion_g", "assumptions", "calories", "protein_g", "carbs_g", "fat_g"],
}

MEAL_MACRO_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "meal_name": {"type": "string"},
        "calories": {"type": "number"},
        "protein_g": {"type": "number"},
        "carbs_g": {"type": "number"},
        "fat_g": {"type": "number"},
        "total_best": MACRO_TOTAL_SCHEMA,
        "total_low": MACRO_TOTAL_SCHEMA,
        "total_high": MACRO_TOTAL_SCHEMA,
        "items": {"type": "array", "items": MEAL_ITEM_SCHEMA, "minItems": 1},
        "variance_drivers": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "notes": {"type": "string"},
    },
    "required": [
        "meal_name",
        "calories",
        "protein_g",
        "carbs_g",
        "fat_g",
        "total_best",
        "total_low",
        "total_high",
        "items",
        "variance_drivers",
        "confidence",
        "notes",
    ],
}

RECOMMENDATION_CHOICE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "food_id": {"type": "string"},
        "fit_rationale": {"type": "string"},
        "tradeoffs": {"type": "string"},
    },
    "required": ["food_id", "fit_rationale", "tradeoffs"],
}

RECOMMENDATION_SELECTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "suggestions": {
            "type": "array",
            "items": RECOMMENDATION_CHOICE_SCHEMA,
            "minItems": 1,
            "maxItems": 3,
        },
    },
    "required": ["summary", "suggestions"],
}

CATALOG_OVERLAP_DECISION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "suggestion_id": {"type": "string"},
        "action": {"type": "string", "enum": ["keep", "reject_duplicate"]},
        "duplicate_food_id": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["suggestion_id", "action", "duplicate_food_id", "rationale"],
}

CATALOG_OVERLAP_REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decisions": {
            "type": "array",
            "items": CATALOG_OVERLAP_DECISION_SCHEMA,
            "minItems": 1,
        }
    },
    "required": ["decisions"],
}

PORTION_REFERENCE_GUIDELINES = [
    "1 cup cooked rice = 200g",
    "1 egg = 50g",
    "1 tbsp oil = 120 kcal (about 14g fat)",
    "1 chicken leg (thigh + drumstick) = 200g edible portion",
    "1 cup cooked vegetables = 100g",
]

PROMPT_TEMPLATE = (
    "You are a nutrition analyst. Analyze the meal in the image and caption.\n"
    "Follow this process exactly:\n"
    "1) Identify all visible food items.\n"
    "2) Estimate portion size in grams using standard serving references.\n"
    "3) State cooking assumptions (oil, skin, sauces, frying).\n"
    "4) Calculate calories, protein, carbs, and fat for each item.\n"
    "5) Provide total macros with best estimate, low estimate, and high estimate.\n"
    "6) Provide variance drivers for the range (portion size, oil/fat, sauces, hidden ingredients).\n"
    "\n"
    "Portion reference guidelines:\n"
    "- {portion_guidelines}\n"
    "\n"
    "Oil rules:\n"
    "- If foods appear fried, assume 1-2 tsp cooking oil unless clearly low-oil.\n"
    "- If fried rice appears, assume 1 tbsp oil unless caption says otherwise.\n"
    "- Use mild conservative defaults for hidden cooking fat when unclear and explain assumptions.\n"
    "\n"
    "Output requirements:\n"
    "- Return structured JSON only following the provided schema.\n"
    "- totals must satisfy low <= best <= high for each macro.\n"
    "- Keep top-level calories/protein/carbs/fat aligned with total_best for backward compatibility.\n"
    "\n"
    "Caption: {caption}\n"
    "Persona context (optional): {persona_hint}\n"
)

RECOMMENDATION_PROMPT_TEMPLATE = (
    "You are ranking next-meal candidates for a macro-tracking user.\n"
    "Use ONLY the provided candidate foods. Do not invent foods, portions, or macros.\n"
    "Pick the best 3 candidates for the next meal based on remaining macros, dietary restrictions, "
    "preferred cuisines, and recent meal repetition.\n"
    "Keep explanations concise and practical.\n"
    "\n"
    "User: {person}\n"
    "Profile: {profile}\n"
    "Today totals: {today_totals}\n"
    "Remaining macros: {remaining_macros}\n"
    "Recent meals: {recent_meals}\n"
    "\n"
    "Candidate foods:\n"
    "{candidate_foods}\n"
)

CATALOG_OVERLAP_PROMPT_TEMPLATE = (
    "You are reviewing suggested food catalog entries against an existing catalog.\n"
    "For each suggestion, decide whether it should be kept for manual review or rejected as a duplicate.\n"
    "Reject only if it is effectively the same recurring meal as an existing catalog entry, even with minor wording "
    "or brand differences. Do not reject items that are only in the same broad category but clearly different meals.\n"
    "Return one decision per suggestion.\n"
    "\n"
    "Existing catalog entries:\n"
    "{catalog_entries}\n"
    "\n"
    "Suggestions to review:\n"
    "{suggestions}\n"
)

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
logger.info("macro_api startup: OPENAI_API_KEY loaded=%s", bool(OPENAI_API_KEY))
app.mount("/miniapp/static", StaticFiles(directory=MINIAPP_DIR), name="miniapp-static")


def _mini_app_base_url() -> str:
    if TELEGRAM_WEBHOOK_BASE_URL:
        return TELEGRAM_WEBHOOK_BASE_URL.rstrip("/")
    if MINI_APP_URL.endswith("/miniapp"):
        return MINI_APP_URL[: -len("/miniapp")]
    return MINI_APP_URL.rstrip("/")


def _telegram_webhook_url() -> str:
    base_url = _mini_app_base_url()
    if not base_url:
        return ""
    return f"{base_url}{TELEGRAM_WEBHOOK_PATH}"


async def _start_telegram_webhook() -> None:
    webhook_url = _telegram_webhook_url()
    if not BOT_TOKEN or not webhook_url:
        logger.info(
            "telegram_webhook disabled bot_token=%s webhook_url=%s",
            bool(BOT_TOKEN),
            bool(webhook_url),
        )
        app.state.telegram_application = None
        return

    telegram_application = build_telegram_application(create_updater=False)
    await telegram_application.initialize()
    await telegram_application.start()
    await telegram_application.bot.set_webhook(
        url=webhook_url,
        secret_token=TELEGRAM_WEBHOOK_SECRET or None,
    )
    app.state.telegram_application = telegram_application
    logger.info("telegram_webhook configured url=%s", webhook_url)


async def _stop_telegram_webhook() -> None:
    telegram_application = getattr(app.state, "telegram_application", None)
    app.state.telegram_application = None
    if telegram_application is None:
        return
    await telegram_application.stop()
    await telegram_application.shutdown()


@app.on_event("startup")
async def app_startup() -> None:
    await _start_telegram_webhook()


@app.on_event("shutdown")
async def app_shutdown() -> None:
    await _stop_telegram_webhook()


def downscale_for_vision(image_bytes: bytes, max_side: int = 768, quality: int = 80) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def _usage_value(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _extract_usage(resp: Any) -> Dict[str, Any]:
    usage = _usage_value(resp, "usage")
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "input_image_tokens": None,
        }

    input_tokens = _usage_value(usage, "input_tokens")
    output_tokens = _usage_value(usage, "output_tokens")
    total_tokens = _usage_value(usage, "total_tokens")
    input_details = _usage_value(usage, "input_tokens_details")
    input_image_tokens = _usage_value(input_details, "image_tokens")
    if input_image_tokens is None:
        input_image_tokens = _usage_value(input_details, "input_image_tokens")

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "input_image_tokens": input_image_tokens,
    }


def _profile_store() -> UserProfileStore:
    return UserProfileStore(USER_PROFILES_PATH)


def _require_telegram_user(init_data: str) -> Dict[str, Any]:
    init_data = (init_data or "").strip()
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing X-Telegram-Init-Data header")
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not set")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.get("hash", "")
    auth_date = parsed.get("auth_date", "")
    user_payload = parsed.get("user", "")

    if not received_hash or not auth_date or not user_payload:
        raise HTTPException(status_code=401, detail="Telegram init data is incomplete")

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(parsed.items())
        if key != "hash"
    )
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise HTTPException(status_code=401, detail="Telegram init data signature is invalid")

    auth_timestamp = int(auth_date)
    current_timestamp = int(datetime.utcnow().timestamp())
    if current_timestamp - auth_timestamp > TELEGRAM_INIT_DATA_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Telegram init data is stale")

    try:
        user = json.loads(user_payload)
    except json.JSONDecodeError as err:
        raise HTTPException(status_code=401, detail="Telegram init data user payload is invalid") from err

    telegram_user_id = int(user.get("id", 0) or 0)
    if telegram_user_id <= 0:
        raise HTTPException(status_code=401, detail="Telegram init data user id is invalid")

    username = str(user.get("username", "") or "")
    first_name = str(user.get("first_name", "") or "").strip()
    last_name = str(user.get("last_name", "") or "").strip()
    display_name = " ".join(part for part in [first_name, last_name] if part).strip() or username or str(telegram_user_id)
    return {
        "telegram_user_id": telegram_user_id,
        "username": username,
        "display_name": display_name,
    }


def _questionnaire_preview_payload(answers: QuestionnaireAnswers) -> Dict[str, Any]:
    activity_labels = {str(item["value"]): str(item["label"]) for item in ACTIVITY_LEVEL_OPTIONS}
    goal_labels = {str(item["value"]): str(item["label"]) for item in GOAL_OPTIONS}
    return {
        "questionnaire_answers": answers.to_payload(),
        "questionnaire_version": QUESTIONNAIRE_VERSION,
        "daily_target": derive_daily_target(answers).to_payload(),
        "activity_label": activity_labels[answers.activity_level],
        "goal_label": goal_labels[answers.goal],
    }


def _profile_response_payload(profile: Optional[UserProfile]) -> Dict[str, Any]:
    payload = {
        "profile": profile.to_payload() if profile is not None else None,
        "questionnaire_version": QUESTIONNAIRE_VERSION,
    }
    payload.update(questionnaire_meta_payload())
    return payload


def _append_estimate_metrics(record: Dict[str, Any]) -> None:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    if ESTIMATES_LOG_PATH.exists() and ESTIMATES_LOG_PATH.stat().st_size > 0:
        with ESTIMATES_LOG_PATH.open("rb+") as metrics_file:
            metrics_file.seek(-1, 2)
            if metrics_file.read(1) != b"\n":
                metrics_file.write(b"\n")
    with ESTIMATES_LOG_PATH.open("a", encoding="utf-8") as metrics_file:
        metrics_file.write(json.dumps(record, ensure_ascii=True))
        metrics_file.write("\n")


def _macro_energy(total: Dict[str, Any]) -> float:
    return (
        float(total["protein_g"]) * 4.0
        + float(total["carbs_g"]) * 4.0
        + float(total["fat_g"]) * 9.0
    )


def _validate_range_constraints(low: Dict[str, Any], best: Dict[str, Any], high: Dict[str, Any]) -> None:
    for key in ("calories", "protein_g", "carbs_g", "fat_g"):
        low_v = float(low[key])
        best_v = float(best[key])
        high_v = float(high[key])
        if not (low_v <= best_v <= high_v):
            raise ValueError(f"Invalid range ordering for {key}: expected low <= best <= high")


def _validate_calorie_consistency(total: Dict[str, Any], label: str) -> None:
    macro_energy = _macro_energy(total)
    calories = float(total["calories"])
    allowed_delta = max(CALORIE_CONSISTENCY_TOLERANCE_KCAL, calories * CALORIE_CONSISTENCY_TOLERANCE_RATIO)
    if abs(calories - macro_energy) > allowed_delta:
        raise ValueError(
            f"{label} calories inconsistent with macro energy (calories={calories}, macro_energy={macro_energy:.1f})"
        )


def _validate_result(result: Dict[str, Any]) -> Dict[str, Any]:
    required = {
        "meal_name",
        "calories",
        "protein_g",
        "carbs_g",
        "fat_g",
        "total_best",
        "total_low",
        "total_high",
        "items",
        "variance_drivers",
        "confidence",
        "notes",
    }
    missing = [key for key in required if key not in result]
    if missing:
        raise ValueError(f"Missing keys in OpenAI response: {', '.join(sorted(missing))}")

    confidence = float(result["confidence"])
    if confidence < 0 or confidence > 1:
        raise ValueError("confidence must be between 0 and 1")

    items = result["items"]
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")

    variance_drivers = result["variance_drivers"]
    if not isinstance(variance_drivers, list) or not variance_drivers:
        raise ValueError("variance_drivers must be a non-empty list")

    for item in items:
        required_item = {"name", "portion_g", "assumptions", "calories", "protein_g", "carbs_g", "fat_g"}
        missing_item = [key for key in required_item if key not in item]
        if missing_item:
            raise ValueError(f"Missing item keys: {', '.join(sorted(missing_item))}")

    total_best = result["total_best"]
    total_low = result["total_low"]
    total_high = result["total_high"]
    _validate_range_constraints(total_low, total_best, total_high)
    _validate_calorie_consistency(total_best, "total_best")
    _validate_calorie_consistency(total_low, "total_low")
    _validate_calorie_consistency(total_high, "total_high")

    result["calories"] = int(round(float(total_best["calories"])))
    result["protein_g"] = round(float(total_best["protein_g"]), 1)
    result["carbs_g"] = round(float(total_best["carbs_g"]), 1)
    result["fat_g"] = round(float(total_best["fat_g"]), 1)
    result["confidence"] = confidence
    result["notes"] = str(result.get("notes", ""))
    result["meal_name"] = str(result.get("meal_name", ""))

    for key in ("total_best", "total_low", "total_high"):
        total = result[key]
        total["calories"] = int(round(float(total["calories"])))
        total["protein_g"] = round(float(total["protein_g"]), 1)
        total["carbs_g"] = round(float(total["carbs_g"]), 1)
        total["fat_g"] = round(float(total["fat_g"]), 1)

    normalized_items = []
    for item in items:
        normalized_items.append(
            {
                "name": str(item["name"]),
                "portion_g": round(float(item["portion_g"]), 1),
                "assumptions": str(item["assumptions"]),
                "calories": int(round(float(item["calories"]))),
                "protein_g": round(float(item["protein_g"]), 1),
                "carbs_g": round(float(item["carbs_g"]), 1),
                "fat_g": round(float(item["fat_g"]), 1),
            }
        )
    result["items"] = normalized_items
    result["variance_drivers"] = [str(x) for x in variance_drivers]

    return result


def _normalize_macro_total(payload: Dict[str, Any]) -> Dict[str, float]:
    required = {"calories", "protein_g", "carbs_g", "fat_g"}
    missing = sorted(required - set(payload.keys()))
    if missing:
        raise ValueError(f"Missing macro total keys: {', '.join(missing)}")
    return {
        "calories": round(float(payload["calories"]), 1),
        "protein_g": round(float(payload["protein_g"]), 1),
        "carbs_g": round(float(payload["carbs_g"]), 1),
        "fat_g": round(float(payload["fat_g"]), 1),
    }


def _validate_recommend_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    required = {"telegram_user_id", "profile", "today_totals", "remaining_macros", "recent_meals", "candidate_foods"}
    missing = sorted(required - set(payload.keys()))
    if missing:
        raise ValueError(f"Missing recommendation request keys: {', '.join(missing)}")

    candidate_foods = payload["candidate_foods"]
    if not isinstance(candidate_foods, list) or not candidate_foods:
        raise ValueError("candidate_foods must be a non-empty list")

    remaining_payload = payload["remaining_macros"]
    if not isinstance(remaining_payload, dict) or "remaining" not in remaining_payload:
        raise ValueError("remaining_macros must include a remaining object")

    _normalize_macro_total(payload["today_totals"])
    _normalize_macro_total(remaining_payload["remaining"])

    return payload


def _format_macro_summary(total: Dict[str, float]) -> str:
    return (
        f"{int(round(total['calories']))} kcal, "
        f"{total['protein_g']:.0f}g protein, "
        f"{total['carbs_g']:.0f}g carbs, "
        f"{total['fat_g']:.0f}g fat"
    )


def _recommendation_summary(today_totals: Dict[str, float], remaining: Dict[str, float]) -> str:
    return (
        f"Today so far: {int(round(today_totals['calories']))} kcal. "
        f"Remaining: {_format_macro_summary(remaining)}."
    )


def _fallback_tradeoff(candidate: Dict[str, Any], remaining: Dict[str, float]) -> str:
    notes = []
    if float(candidate["calories"]) > remaining["calories"] and remaining["calories"] > 0:
        notes.append("slightly heavy on calories")
    if float(candidate["fat_g"]) > max(remaining["fat_g"], 12.0):
        notes.append("fat may run a bit high")
    if float(candidate["protein_g"]) < 20.0 and remaining["protein_g"] > 25.0:
        notes.append("protein top-up may still be needed later")
    if not notes:
        notes.append("balanced enough for the next meal slot")
    return "; ".join(notes)


def _build_recommendation_result(
    *,
    summary: str,
    today_totals: Dict[str, float],
    remaining: Dict[str, float],
    suggestions: list[Dict[str, Any]],
    source: str,
) -> Dict[str, Any]:
    return {
        "summary": summary,
        "today_totals": today_totals,
        "remaining_macros": remaining,
        "suggestions": suggestions,
        "source": source,
    }


def _build_recommendation_fallback(payload: Dict[str, Any], source: str = "deterministic_fallback") -> Dict[str, Any]:
    today_totals = _normalize_macro_total(payload["today_totals"])
    remaining = _normalize_macro_total(payload["remaining_macros"]["remaining"])
    suggestions = []
    for candidate in payload["candidate_foods"][:3]:
        suggestions.append(
            {
                "name": str(candidate["name"]),
                "serving": str(candidate["serving"]),
                "calories": round(float(candidate["calories"]), 1),
                "protein_g": round(float(candidate["protein_g"]), 1),
                "carbs_g": round(float(candidate["carbs_g"]), 1),
                "fat_g": round(float(candidate["fat_g"]), 1),
                "fit_rationale": str(candidate.get("fit_reason") or "Balanced fit for your remaining macros."),
                "tradeoffs": _fallback_tradeoff(candidate, remaining),
            }
        )
    return _build_recommendation_result(
        summary=_recommendation_summary(today_totals, remaining),
        today_totals=today_totals,
        remaining=remaining,
        suggestions=suggestions,
        source=source,
    )


def _build_recommendation_prompt(payload: Dict[str, Any]) -> str:
    today_totals = _normalize_macro_total(payload["today_totals"])
    remaining = _normalize_macro_total(payload["remaining_macros"]["remaining"])
    candidate_lines = []
    for candidate in payload["candidate_foods"]:
        candidate_lines.append(
            "- {food_id}: {name} ({serving}) | {calories} kcal | P {protein_g} / C {carbs_g} / F {fat_g}"
            " | fit_score={fit_score} | fit_reason={fit_reason}".format(
                food_id=str(candidate["food_id"]),
                name=str(candidate["name"]),
                serving=str(candidate["serving"]),
                calories=int(round(float(candidate["calories"]))),
                protein_g=round(float(candidate["protein_g"]), 1),
                carbs_g=round(float(candidate["carbs_g"]), 1),
                fat_g=round(float(candidate["fat_g"]), 1),
                fit_score=round(float(candidate.get("fit_score", 0.0) or 0.0), 2),
                fit_reason=str(candidate.get("fit_reason", "")),
            )
        )

    recent_meals = payload.get("recent_meals") or []
    if not recent_meals:
        recent_meals_text = "none"
    else:
        recent_meals_text = ", ".join(str(item) for item in recent_meals[:6])

    profile = payload["profile"]
    display_name = str(profile.get("display_name", "") or "")
    user_label = display_name or f"user {int(payload['telegram_user_id'])}"

    return RECOMMENDATION_PROMPT_TEMPLATE.format(
        person=f"{user_label} (telegram_user_id={int(payload['telegram_user_id'])})",
        profile=json.dumps(payload["profile"], ensure_ascii=True),
        today_totals=_format_macro_summary(today_totals),
        remaining_macros=_format_macro_summary(remaining),
        recent_meals=recent_meals_text,
        candidate_foods="\n".join(candidate_lines),
    )


def _validate_recommendation_selection(
    payload: Dict[str, Any],
    candidate_foods: list[Dict[str, Any]],
) -> Dict[str, Any]:
    required = {"summary", "suggestions"}
    missing = sorted(required - set(payload.keys()))
    if missing:
        raise ValueError(f"Missing recommendation response keys: {', '.join(missing)}")

    suggestions_payload = payload["suggestions"]
    if not isinstance(suggestions_payload, list) or not suggestions_payload:
        raise ValueError("suggestions must be a non-empty list")

    by_id = {str(candidate["food_id"]): candidate for candidate in candidate_foods}
    normalized = []
    seen = set()
    for suggestion in suggestions_payload:
        food_id = str(suggestion.get("food_id", ""))
        if not food_id or food_id not in by_id or food_id in seen:
            continue
        candidate = by_id[food_id]
        normalized.append(
            {
                "name": str(candidate["name"]),
                "serving": str(candidate["serving"]),
                "calories": round(float(candidate["calories"]), 1),
                "protein_g": round(float(candidate["protein_g"]), 1),
                "carbs_g": round(float(candidate["carbs_g"]), 1),
                "fat_g": round(float(candidate["fat_g"]), 1),
                "fit_rationale": str(suggestion["fit_rationale"]),
                "tradeoffs": str(suggestion["tradeoffs"]),
            }
        )
        seen.add(food_id)

    if not normalized:
        raise ValueError("OpenAI recommendation response did not reference valid candidates")

    return {
        "summary": str(payload["summary"]),
        "suggestions": normalized[:3],
    }


def _validate_catalog_overlap_review_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    required = {"suggestions", "catalog_entries"}
    missing = sorted(required - set(payload.keys()))
    if missing:
        raise ValueError(f"Missing catalog overlap request keys: {', '.join(missing)}")
    if not isinstance(payload["suggestions"], list) or not payload["suggestions"]:
        raise ValueError("suggestions must be a non-empty list")
    if not isinstance(payload["catalog_entries"], list):
        raise ValueError("catalog_entries must be a list")
    return payload


def _build_catalog_overlap_prompt(payload: Dict[str, Any]) -> str:
    catalog_lines = []
    for entry in payload["catalog_entries"]:
        catalog_lines.append(
            "- {food_id}: {name} ({serving}) | {calories} kcal | P {protein_g} / C {carbs_g} / F {fat_g} | eligible_user_ids={eligible_user_ids}".format(
                food_id=str(entry.get("food_id", "")),
                name=str(entry.get("name", "")),
                serving=str(entry.get("serving", "")),
                calories=int(round(float(entry.get("macros", {}).get("calories", 0) or 0))),
                protein_g=round(float(entry.get("macros", {}).get("protein_g", 0) or 0), 1),
                carbs_g=round(float(entry.get("macros", {}).get("carbs_g", 0) or 0), 1),
                fat_g=round(float(entry.get("macros", {}).get("fat_g", 0) or 0), 1),
                eligible_user_ids=",".join(
                    str(x) for x in entry.get("eligible_telegram_user_ids", [])
                )
                or "all",
            )
        )

    suggestion_lines = []
    for item in payload["suggestions"]:
        suggestion_lines.append(
            "- {suggestion_id}: {name} ({serving}) | {calories} kcal | P {protein_g} / C {carbs_g} / F {fat_g} "
            "| telegram_user_id={telegram_user_id} | source_captions={source_captions}".format(
                suggestion_id=str(item.get("suggestion_id", "")),
                name=str(item.get("proposed_name", "")),
                serving=str(item.get("proposed_serving", "")),
                calories=int(round(float(item.get("macros", {}).get("calories", 0) or 0))),
                protein_g=round(float(item.get("macros", {}).get("protein_g", 0) or 0), 1),
                carbs_g=round(float(item.get("macros", {}).get("carbs_g", 0) or 0), 1),
                fat_g=round(float(item.get("macros", {}).get("fat_g", 0) or 0), 1),
                telegram_user_id=int(item.get("telegram_user_id", 0) or 0),
                source_captions=" | ".join(str(x) for x in item.get("source_captions", [])[:3]) or "none",
            )
        )

    return CATALOG_OVERLAP_PROMPT_TEMPLATE.format(
        catalog_entries="\n".join(catalog_lines) or "- none",
        suggestions="\n".join(suggestion_lines),
    )


def _validate_catalog_overlap_response(
    payload: Dict[str, Any],
    suggestions: list[Dict[str, Any]],
    catalog_entries: list[Dict[str, Any]],
) -> Dict[str, Any]:
    decisions = payload.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("decisions must be a non-empty list")

    suggestion_ids = {str(item.get("suggestion_id", "")) for item in suggestions}
    catalog_ids = {str(item.get("food_id", "")) for item in catalog_entries}
    normalized = []
    seen = set()
    for item in decisions:
        suggestion_id = str(item.get("suggestion_id", ""))
        action = str(item.get("action", ""))
        duplicate_food_id = str(item.get("duplicate_food_id", "") or "")
        rationale = str(item.get("rationale", ""))
        if suggestion_id not in suggestion_ids or suggestion_id in seen:
            continue
        if action not in {"keep", "reject_duplicate"}:
            continue
        if action == "reject_duplicate" and duplicate_food_id not in catalog_ids:
            continue
        if action == "keep":
            duplicate_food_id = ""
        normalized.append(
            {
                "suggestion_id": suggestion_id,
                "action": action,
                "duplicate_food_id": duplicate_food_id,
                "rationale": rationale,
            }
        )
        seen.add(suggestion_id)

    if not normalized:
        raise ValueError("catalog overlap review produced no valid decisions")

    for suggestion_id in suggestion_ids:
        if suggestion_id not in seen:
            normalized.append(
                {
                    "suggestion_id": suggestion_id,
                    "action": "keep",
                    "duplicate_food_id": "",
                    "rationale": "No valid model decision returned; kept for manual review.",
                }
            )

    return {"decisions": normalized}


@app.post("/estimate")
async def estimate(
    file: UploadFile,
    caption: str = Form(""),
    persona_hint: str = Form(""),
    experiment_id: str = Form(""),
    model_override: str = Form(""),
    vision_detail_override: str = Form(""),
    max_side_override: int = Form(0),
) -> Dict[str, Any]:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty upload")

    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Image too large; please resend a smaller photo")

    if not OPENAI_API_KEY or client is None:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not set")

    run_model = (model_override or "").strip() or MODEL
    run_vision_detail = (vision_detail_override or "").strip().lower() or VISION_DETAIL
    if run_vision_detail not in ALLOWED_VISION_DETAILS:
        raise HTTPException(status_code=400, detail="vision_detail_override must be 'low' or 'high'")
    run_max_side = max_side_override if max_side_override and max_side_override > 0 else VISION_MAX_SIDE

    try:
        img_bytes = downscale_for_vision(raw, max_side=run_max_side, quality=80)
    except Exception as err:
        logger.exception("Image preprocessing failed")
        raise HTTPException(status_code=400, detail=f"Invalid image: {str(err)[:120]}") from err

    b64 = base64.b64encode(img_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"
    prompt = PROMPT_TEMPLATE.format(
        caption=caption,
        persona_hint=persona_hint or "none",
        portion_guidelines="\n- ".join(PORTION_REFERENCE_GUIDELINES),
    )

    try:
        resp = client.responses.create(
            model=run_model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": data_url, "detail": run_vision_detail},
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "meal_macros",
                    "schema": MEAL_MACRO_SCHEMA,
                    "strict": True,
                }
            },
        )
        usage = _extract_usage(resp)
        logger.info(
            "token_usage model=%s input_tokens=%s output_tokens=%s total_tokens=%s image_tokens=%s "
            "prompt_chars=%s raw_image_bytes=%s resized_image_bytes=%s vision_detail=%s",
            run_model,
            usage["input_tokens"],
            usage["output_tokens"],
            usage["total_tokens"],
            usage["input_image_tokens"],
            len(prompt),
            len(raw),
            len(img_bytes),
            run_vision_detail,
        )
        output_text = getattr(resp, "output_text", None)
        if not output_text:
            raise ValueError("OpenAI response did not include output_text")
        result = json.loads(output_text)
        validated = _validate_result(result)

        logger.info(
            "macro_estimate meal=%s kcal=%s range=%s-%s confidence=%.2f variance_drivers=%s",
            validated.get("meal_name"),
            validated.get("calories"),
            validated.get("total_low", {}).get("calories"),
            validated.get("total_high", {}).get("calories"),
            validated.get("confidence"),
            "; ".join(validated.get("variance_drivers", [])[:3]),
        )
        try:
            metrics_record = {
                "event_type": "estimate",
                "event_id": str(uuid4()),
                "timestamp_utc": datetime.utcnow().isoformat(timespec="seconds"),
                "model": run_model,
                "vision_detail": run_vision_detail,
                "usage": {
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "total_tokens": usage["total_tokens"],
                    "input_image_tokens": usage["input_image_tokens"],
                },
                "request": {
                    "experiment_id": (experiment_id or "").strip() or None,
                    "prompt_chars": len(prompt),
                    "raw_image_bytes": len(raw),
                    "resized_image_bytes": len(img_bytes),
                    "caption": caption,
                    "persona_hint_used": bool(persona_hint.strip()),
                    "max_side": run_max_side,
                },
                "estimate": {
                    "meal_name": validated.get("meal_name"),
                    "calories": validated.get("calories"),
                    "protein_g": validated.get("protein_g"),
                    "carbs_g": validated.get("carbs_g"),
                    "fat_g": validated.get("fat_g"),
                    "total_low": validated.get("total_low"),
                    "total_high": validated.get("total_high"),
                    "confidence": validated.get("confidence"),
                    "variance_drivers": validated.get("variance_drivers"),
                    "item_count": len(validated.get("items", [])),
                },
            }
            _append_estimate_metrics(metrics_record)
            validated["metrics_event_id"] = metrics_record["event_id"]
        except Exception as metrics_err:
            logger.warning("metrics_logging_failed detail=%s", str(metrics_err)[:120])

        return validated
    except HTTPException:
        raise
    except json.JSONDecodeError as err:
        logger.exception("Failed parsing OpenAI JSON response")
        raise HTTPException(status_code=502, detail=f"OpenAI response parse failed: {str(err)[:120]}") from err
    except ValueError as err:
        logger.exception("OpenAI response validation failed")
        raise HTTPException(status_code=502, detail=f"OpenAI response validation failed: {str(err)[:120]}") from err
    except Exception as err:
        logger.exception("OpenAI call failed")
        raise HTTPException(status_code=502, detail=f"OpenAI call failed: {str(err)[:120]}") from err


@app.post("/recommend")
async def recommend(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        validated_payload = _validate_recommend_request(payload)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    fallback = _build_recommendation_fallback(validated_payload)
    if not OPENAI_API_KEY or client is None:
        logger.warning("recommendation_model_unavailable using_fallback=true")
        return fallback

    prompt = _build_recommendation_prompt(validated_payload)
    try:
        resp = client.responses.create(
            model=RECOMMEND_MODEL,
            input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "meal_recommendations",
                    "schema": RECOMMENDATION_SELECTION_SCHEMA,
                    "strict": True,
                }
            },
        )
        usage = _extract_usage(resp)
        logger.info(
            "recommend_usage model=%s input_tokens=%s output_tokens=%s total_tokens=%s prompt_chars=%s",
            RECOMMEND_MODEL,
            usage["input_tokens"],
            usage["output_tokens"],
            usage["total_tokens"],
            len(prompt),
        )
        output_text = getattr(resp, "output_text", None)
        if not output_text:
            raise ValueError("OpenAI response did not include output_text")
        selection = _validate_recommendation_selection(
            json.loads(output_text),
            validated_payload["candidate_foods"],
        )
        today_totals = _normalize_macro_total(validated_payload["today_totals"])
        remaining = _normalize_macro_total(validated_payload["remaining_macros"]["remaining"])
        result = _build_recommendation_result(
            summary=selection["summary"],
            today_totals=today_totals,
            remaining=remaining,
            suggestions=selection["suggestions"],
            source="model_ranked",
        )
        logger.info(
            "recommendation_result telegram_user_id=%s source=%s suggestions=%s",
            validated_payload["telegram_user_id"],
            result["source"],
            ", ".join(item["name"] for item in result["suggestions"]),
        )
        return result
    except Exception as err:
        logger.warning(
            "recommendation_fallback telegram_user_id=%s detail=%s",
            validated_payload["telegram_user_id"],
            str(err)[:120],
        )
        fallback["source"] = "deterministic_fallback"
        return fallback


@app.post("/catalog/review-overlaps")
async def review_catalog_overlaps(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        validated_payload = _validate_catalog_overlap_review_request(payload)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    if not OPENAI_API_KEY or client is None:
        decisions = [
            {
                "suggestion_id": str(item["suggestion_id"]),
                "action": "keep",
                "duplicate_food_id": "",
                "rationale": "Model unavailable; kept for manual review.",
            }
            for item in validated_payload["suggestions"]
        ]
        return {"decisions": decisions}

    prompt = _build_catalog_overlap_prompt(validated_payload)
    try:
        resp = client.responses.create(
            model=CATALOG_REVIEW_MODEL,
            input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "catalog_overlap_review",
                    "schema": CATALOG_OVERLAP_REVIEW_SCHEMA,
                    "strict": True,
                }
            },
        )
        usage = _extract_usage(resp)
        logger.info(
            "catalog_overlap_review_usage model=%s input_tokens=%s output_tokens=%s total_tokens=%s prompt_chars=%s",
            CATALOG_REVIEW_MODEL,
            usage["input_tokens"],
            usage["output_tokens"],
            usage["total_tokens"],
            len(prompt),
        )
        output_text = getattr(resp, "output_text", None)
        if not output_text:
            raise ValueError("OpenAI response did not include output_text")
        result = _validate_catalog_overlap_response(
            json.loads(output_text),
            validated_payload["suggestions"],
            validated_payload["catalog_entries"],
        )
        logger.info(
            "catalog_overlap_review_decisions suggestions=%s rejected=%s",
            len(result["decisions"]),
            sum(1 for item in result["decisions"] if item["action"] == "reject_duplicate"),
        )
        return result
    except Exception as err:
        logger.warning("catalog_overlap_review_fallback detail=%s", str(err)[:120])
        return {
            "decisions": [
                {
                    "suggestion_id": str(item["suggestion_id"]),
                    "action": "keep",
                    "duplicate_food_id": "",
                    "rationale": "Review failed; kept for manual review.",
                }
                for item in validated_payload["suggestions"]
            ]
        }


@app.post(TELEGRAM_WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default="", alias="X-Telegram-Bot-Api-Secret-Token"),
) -> Dict[str, Any]:
    if TELEGRAM_WEBHOOK_SECRET and x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")

    telegram_application = getattr(app.state, "telegram_application", None)
    if telegram_application is None:
        raise HTTPException(status_code=503, detail="Telegram webhook is not configured")

    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Telegram update payload")

    update = Update.de_json(payload, telegram_application.bot)
    await telegram_application.process_update(update)
    return {"ok": True}


@app.get("/miniapp")
async def miniapp_index() -> FileResponse:
    return FileResponse(MINIAPP_DIR / "index.html")


@app.get("/miniapp/api/profile")
async def miniapp_profile(
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> Dict[str, Any]:
    auth_user = _require_telegram_user(x_telegram_init_data)
    store = _profile_store()
    try:
        profile = store.get(auth_user["telegram_user_id"])
    except KeyError:
        profile = None
    return _profile_response_payload(profile)


@app.post("/miniapp/api/targets/preview")
async def preview_macro_targets(
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> Dict[str, Any]:
    _require_telegram_user(x_telegram_init_data)
    try:
        answers = QuestionnaireAnswers.from_payload(payload)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return _questionnaire_preview_payload(answers)


@app.post("/miniapp/api/profile")
async def save_miniapp_profile(
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> Dict[str, Any]:
    auth_user = _require_telegram_user(x_telegram_init_data)
    try:
        answers = QuestionnaireAnswers.from_payload(payload)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    store = _profile_store()
    try:
        existing = store.get(auth_user["telegram_user_id"])
    except KeyError:
        existing = None

    profile = UserProfile(
        telegram_user_id=auth_user["telegram_user_id"],
        username=auth_user["username"] or (existing.username if existing is not None else ""),
        display_name=auth_user["display_name"] or (existing.display_name if existing is not None else ""),
        daily_target=derive_daily_target(answers),
        questionnaire_answers=answers,
        questionnaire_version=QUESTIONNAIRE_VERSION,
        updated_at=datetime.utcnow().isoformat(timespec="seconds"),
        dietary_preferences=list(existing.dietary_preferences) if existing is not None else [],
        restrictions=list(existing.restrictions) if existing is not None else [],
        preferred_cuisines=list(existing.preferred_cuisines) if existing is not None else [],
        preferred_staples=list(existing.preferred_staples) if existing is not None else [],
        preferred_tags=list(existing.preferred_tags) if existing is not None else [],
    )
    store.upsert(profile)
    response_payload = _profile_response_payload(profile)
    response_payload["preview"] = _questionnaire_preview_payload(answers)
    return response_payload
