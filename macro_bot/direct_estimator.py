"""Direct OpenAI image estimation used by the Lambda worker.

This intentionally mirrors the local API's prompt, schema, preprocessing, and
validation rules, but does not import the FastAPI application and never writes
repository metrics files.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from PIL import Image

from .models import ASSUMPTION_KEYS, ESTIMATOR_VERSION, ITEM_EVIDENCE_LEVELS, MacroTotal, MealEstimate
from .serverless_auth import SSMParameterCache, openai_key_from_environment

logger = logging.getLogger(__name__)

MODEL = "gpt-5.4"
ESTIMATOR_APPLICATION_VERSION = ESTIMATOR_VERSION
VISION_DETAIL = "high"
VISION_MAX_SIDE = 768
MAX_UPLOAD_BYTES = 6_000_000
ALLOWED_VISION_DETAILS = {"low", "high"}
# One application-level retry with a 45-second deadline leaves headroom inside
# the worker's 120-second Lambda budget. The OpenAI SDK retry layer is disabled
# so retries are not multiplied.
OPENAI_REQUEST_TIMEOUT_SECONDS = 45.0
OPENAI_APPLICATION_RETRIES = 1
OPENAI_SDK_RETRIES = 0

StageTelemetry = Callable[[str, int, str, Optional[int], Optional[str]], None]

PORTION_REFERENCE_GUIDELINES = [
    "1 cup cooked rice = 200g",
    "1 egg = 50g",
    "1 tbsp oil = 120 kcal (about 14g fat)",
    "1 chicken leg (thigh + drumstick) = 200g edible portion",
    "1 cup cooked vegetables = 100g",
]

PROMPT_TEMPLATE = (
    "You are a careful nutrition analyst for real-world meal photos, including Singaporean, Indian, and Asian meals. Analyze the meal in the image and caption.\n"
    "Follow this process exactly:\n"
    "1) Identify all visible food items.\n"
    "2) Estimate portion size in grams using standard serving references.\n"
    "3) State cooking assumptions (oil, skin, sauces, frying).\n"
    "4) Calculate calories, protein, carbs, and fat for each item.\n"
    "5) Provide total macros with best estimate, low estimate, and high estimate.\n"
    "6) Provide variance drivers for the range (portion size, oil/fat, sauces, hidden ingredients).\n"
    "7) Classify each component by visual evidence and separately score food identification, portion, and macro confidence.\n"
    "8) Separate explicit caption facts, visible observations, and weak inferences.\n"
    "\n"
    "Portion reference guidelines:\n"
    "- {portion_guidelines}\n"
    "\n"
    "Oil rules:\n"
    "- If foods appear fried, estimate absorbed oil, but widen the range when the amount is not visible.\n"
    "- Do not apply cuisine stereotypes automatically: a curry, rice, noodle, egg, chicken, gravy, sauce, beverage, or fried item must be supported by the current image/caption.\n"
    "- Account explicitly for visible creamy or oily sauce; widen the range when its quantity is unclear.\n"
    "- Treat hidden cooking oil as an uncertainty driver rather than silently adding it or ignoring it.\n"
    "\n"
    "Visual grounding rules:\n"
    "- Evidence hierarchy: (1) explicit user caption facts, (2) clear current image evidence, (3) visually plausible inference, (4) historical priors only as weak priors. Never let a historical prior override current evidence.\n"
    "- Caption facts such as grams, number of eggs, half rice, skin removed, no sauce, or little oil materially change the estimate and must not trigger a duplicate question.\n"
    "- Identify only food components visually supported by the image.\n"
    "- If a component is hidden or ambiguous, mark it as inferred, partially_occluded, or uncertain.\n"
    "- Do not confidently assert a hidden noodle/rice base, egg, topping, or sauce merely because it is common for the meal category.\n"
    "- Distinguish visible food from likely hidden ingredients and explain the uncertainty.\n"
    "- For each major component, provide portion_low_g, portion_g, and portion_high_g; widen this range when depth or occlusion prevents precision.\n"
    "- Set item_breakdown_complete=false when hidden ingredients, aggregate-only oil, or an incomplete item list cannot be assigned to items.\n"
    "- Set item_breakdown_complete=true only when the listed items cover the estimated meal contribution.\n"
    "- For each item, fill assumption_categories for food_identity, portion, cooking_method, oil_fat, sauce_dressing, and hidden_ingredients. Use null when not applicable.\n"
    "\n"
    "Output requirements:\n"
    "- Return structured JSON only following the provided schema.\n"
    "- totals must satisfy low <= best <= high for each macro.\n"
    "- Keep top-level calories/protein/carbs/fat aligned with total_best for backward compatibility.\n"
    "- Use null for a confidence or portion-range field that cannot be judged reliably.\n"
    "- Evidence must be one of clearly_visible, probably_visible, partially_occluded, inferred, or uncertain.\n"
    "- Do not invent a follow-up question; the application will add one deterministically only for high-impact ambiguity.\n"
    "\n"
    "Caption: {caption}\n"
    "Caption facts parsed by the application: {caption_facts}\n"
    "Persona context (optional): {persona_hint}\n"
)

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
        "assumption_categories": {
            "type": "object",
            "additionalProperties": False,
            "properties": {key: {"type": ["string", "null"]} for key in ASSUMPTION_KEYS},
            "required": list(ASSUMPTION_KEYS),
        },
        "calories": {"type": "number"},
        "protein_g": {"type": "number"},
        "carbs_g": {"type": "number"},
        "fat_g": {"type": "number"},
        "portion_low_g": {"type": ["number", "null"]},
        "portion_high_g": {"type": ["number", "null"]},
        "identification_confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "portion_confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "evidence": {"type": "string", "enum": sorted(ITEM_EVIDENCE_LEVELS)},
    },
    "required": [
        "name", "portion_g", "assumptions", "assumption_categories", "calories", "protein_g", "carbs_g", "fat_g",
        "portion_low_g", "portion_high_g", "identification_confidence", "portion_confidence", "evidence",
    ],
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
        "identification_confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "portion_confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "macro_confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "item_breakdown_complete": {"type": "boolean"},
        "total_best": MACRO_TOTAL_SCHEMA,
        "total_low": MACRO_TOTAL_SCHEMA,
        "total_high": MACRO_TOTAL_SCHEMA,
        "items": {"type": "array", "items": MEAL_ITEM_SCHEMA, "minItems": 1},
        "variance_drivers": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "notes": {"type": "string"},
        "estimator_version": {"type": "string"},
    },
    "required": [
        "meal_name", "calories", "protein_g", "carbs_g", "fat_g", "total_best",
        "total_low", "total_high", "items", "variance_drivers", "confidence", "notes",
        "identification_confidence", "portion_confidence", "macro_confidence", "item_breakdown_complete", "estimator_version",
    ],
}


class DirectEstimationError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class EstimationResult:
    estimate: MealEstimate
    model: str
    usage: dict[str, Any]


def downscale_for_vision(image_bytes: bytes, max_side: int = VISION_MAX_SIDE, quality: int = 80) -> bytes:
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    image.thumbnail((max_side, max_side))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue()


def _usage_value(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _extract_usage(response: Any) -> dict[str, Any]:
    usage = _usage_value(response, "usage")
    if usage is None:
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None, "input_image_tokens": None}
    input_tokens = _usage_value(usage, "input_tokens")
    output_tokens = _usage_value(usage, "output_tokens")
    total_tokens = _usage_value(usage, "total_tokens")
    details = _usage_value(usage, "input_tokens_details")
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "input_image_tokens": _usage_value(details, "image_tokens") or _usage_value(details, "input_image_tokens"),
    }


def _macro_energy(total: dict[str, Any]) -> float:
    return float(total["protein_g"]) * 4.0 + float(total["carbs_g"]) * 4.0 + float(total["fat_g"]) * 9.0


def _validate_range_constraints(low: dict[str, Any], best: dict[str, Any], high: dict[str, Any]) -> None:
    for key in ("calories", "protein_g", "carbs_g", "fat_g"):
        if not (float(low[key]) <= float(best[key]) <= float(high[key])):
            raise ValueError(f"Invalid range ordering for {key}: expected low <= best <= high")


def _validate_calorie_consistency(total: dict[str, Any], label: str) -> None:
    calories = float(total["calories"])
    allowed_delta = max(120.0, calories * 0.20)
    if abs(calories - _macro_energy(total)) > allowed_delta:
        raise ValueError(f"{label} calories inconsistent with macro energy")


def _item_derived_total(items: list[dict[str, Any]]) -> dict[str, float]:
    return {
        key: round(sum(float(item[key]) for item in items), 1)
        for key in ("calories", "protein_g", "carbs_g", "fat_g")
    }


def _within_reconciliation_tolerance(key: str, reported: float, derived: float) -> bool:
    absolute_tolerance = {"calories": 30.0, "protein_g": 3.0, "carbs_g": 3.0, "fat_g": 2.0}[key]
    return abs(float(reported) - float(derived)) <= max(absolute_tolerance, abs(float(reported)) * 0.05)


def _is_calorie_consistent(total: dict[str, Any]) -> bool:
    try:
        _validate_calorie_consistency(total, "item_derived_total")
    except ValueError:
        return False
    return True


def _needs_follow_up(item: dict[str, Any]) -> bool:
    evidence = str(item.get("evidence", "uncertain") or "uncertain")
    if evidence in {"clearly_visible", "probably_visible"}:
        return False
    for key in ("identification_confidence", "portion_confidence"):
        value = item.get(key)
        if value is not None and float(value) >= 0.65:
            continue
        return True
    return True


def extract_caption_facts(caption: str) -> list[str]:
    """Extract explicit high-signal facts without inferring user habits."""

    import re

    text = " ".join(str(caption or "").lower().split())
    facts: list[str] = []
    if any(term in text for term in ("skin removed", "without skin", "no skin")):
        facts.append("skin_removed")
    if "no sauce" in text or "without sauce" in text:
        facts.append("no_sauce")
    if "little oil" in text or "low oil" in text:
        facts.append("low_oil")
    if "half rice" in text or "half portion rice" in text:
        facts.append("half_rice")
    if "half noodles" in text or "half portion noodles" in text:
        facts.append("half_noodles")
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*g\s+(?:of\s+)?([a-z][a-z ]{1,30})", text):
        facts.append(f"{match.group(2).strip()}={match.group(1)}g")
    egg_match = re.search(r"(\d+)\s+eggs?\b", text)
    if egg_match:
        facts.append(f"eggs={egg_match.group(1)}")
    return facts[:8]


def _uncertainty_impact(item: dict[str, Any]) -> float:
    low = item.get("portion_low_g")
    high = item.get("portion_high_g")
    if low is None or high is None:
        return 0.0
    width = max(0.0, float(high) - float(low))
    portion = max(float(item.get("portion_g", 1.0)), 1.0)
    return width * max(0.1, float(item.get("calories", 0.0)) / portion)


def _follow_up_question(result: dict[str, Any]) -> Optional[str]:
    base_terms = ("noodle", "rice", "pasta", "grain", "soba", "couscous", "quinoa")
    sauce_terms = ("sauce", "mayo", "mayonnaise", "dressing", "oil", "glaze", "cheese", "nuts")
    for item in result["items"]:
        name = str(item.get("name", "")).lower()
        if any(term in name for term in base_terms) and _needs_follow_up(item) and _uncertainty_impact(item) >= 80:
            return "I can’t clearly see the base under the toppings. Is there noodles, rice, or another grain underneath?"
    for item in result["items"]:
        name = str(item.get("name", "")).lower()
        if any(term in name for term in sauce_terms) and _needs_follow_up(item) and _uncertainty_impact(item) >= 50:
            return "How much sauce or dressing was added: a light drizzle or a generous serving?"
    if not result.get("item_breakdown_complete", True):
        drivers = " ".join(str(value).lower() for value in result.get("variance_drivers", []))
        if any(term in drivers for term in base_terms) and any(_uncertainty_impact(item) >= 80 for item in result["items"]):
            return "I can’t fully account for a hidden base. Is there noodles, rice, or another grain underneath?"
        if any(term in drivers for term in sauce_terms) and any(_uncertainty_impact(item) >= 50 for item in result["items"]):
            return "How much sauce or dressing was added: a light drizzle or a generous serving?"
    return None


def _reconcile_result(result: dict[str, Any]) -> dict[str, Any]:
    model_reported = {key: float(result["total_best"][key]) for key in ("calories", "protein_g", "carbs_g", "fat_g")}
    derived = _item_derived_total(result["items"])
    complete = result.get("item_breakdown_complete", True)
    if not isinstance(complete, bool):
        raise ValueError("item_breakdown_complete must be boolean")

    if complete:
        mismatches = [
            key for key in model_reported
            if not _within_reconciliation_tolerance(key, model_reported[key], derived[key])
        ]
        if _is_calorie_consistent(derived):
            canonical = derived
            status = "reconciled_from_items" if mismatches else "matched"
        else:
            # Do not create a new contradictory canonical total from item
            # fields that do not themselves agree with macro energy.
            canonical = model_reported
            status = "reconciliation_required"
    else:
        canonical = model_reported
        status = "partial_item_breakdown"

    low = dict(result["total_low"])
    high = dict(result["total_high"])
    for key in canonical:
        low[key] = min(float(low[key]), float(canonical[key]))
        high[key] = max(float(high[key]), float(canonical[key]))

    reconciled = dict(result)
    reconciled["model_reported_total"] = model_reported
    reconciled["item_derived_total"] = derived
    reconciled["reconciliation_status"] = status
    reconciled["item_breakdown_complete"] = complete
    reconciled["total_low"] = low
    reconciled["total_high"] = high
    reconciled["total_best"] = canonical
    reconciled["follow_up_question"] = _follow_up_question(reconciled)
    for key in canonical:
        reconciled[key] = canonical[key]
    return reconciled


def validate_result(result: dict[str, Any]) -> dict[str, Any]:
    required = {
        "meal_name", "calories", "protein_g", "carbs_g", "fat_g", "total_best",
        "total_low", "total_high", "items", "variance_drivers", "confidence", "notes",
    }
    missing = sorted(required - set(result))
    if missing:
        raise ValueError(f"Missing keys in OpenAI response: {', '.join(missing)}")
    confidence = float(result["confidence"])
    if confidence < 0 or confidence > 1:
        raise ValueError("confidence must be between 0 and 1")
    if not isinstance(result["items"], list) or not result["items"]:
        raise ValueError("items must be a non-empty list")
    if not isinstance(result["variance_drivers"], list) or not result["variance_drivers"]:
        raise ValueError("variance_drivers must be a non-empty list")
    for item in result["items"]:
        missing_item = sorted({"name", "portion_g", "assumptions", "calories", "protein_g", "carbs_g", "fat_g"} - set(item))
        if missing_item:
            raise ValueError(f"Missing item keys: {', '.join(missing_item)}")
        evidence = str(item.get("evidence", "uncertain") or "uncertain")
        if evidence not in ITEM_EVIDENCE_LEVELS:
            raise ValueError(f"Invalid item evidence level: {evidence}")
        for confidence_name in ("identification_confidence", "portion_confidence"):
            confidence_value = item.get(confidence_name)
            if confidence_value is not None and not 0 <= float(confidence_value) <= 1:
                raise ValueError(f"{confidence_name} must be within [0,1]")
        portion = float(item["portion_g"])
        portion_low = item.get("portion_low_g")
        portion_high = item.get("portion_high_g")
        if portion_low is not None and portion_high is not None:
            if float(portion_low) > float(portion_high) or not (float(portion_low) <= portion <= float(portion_high)):
                raise ValueError("portion_g must be within portion_low_g and portion_high_g")
    _validate_range_constraints(result["total_low"], result["total_best"], result["total_high"])
    _validate_calorie_consistency(result["total_best"], "total_best")
    _validate_calorie_consistency(result["total_low"], "total_low")
    _validate_calorie_consistency(result["total_high"], "total_high")
    result = dict(result)
    result["meal_name"] = str(result.get("meal_name", ""))
    result["calories"] = int(round(float(result["total_best"]["calories"])))
    result["protein_g"] = round(float(result["total_best"]["protein_g"]), 1)
    result["carbs_g"] = round(float(result["total_best"]["carbs_g"]), 1)
    result["fat_g"] = round(float(result["total_best"]["fat_g"]), 1)
    result["confidence"] = confidence
    result["notes"] = str(result.get("notes", ""))
    result["estimator_version"] = str(result.get("estimator_version", ESTIMATOR_APPLICATION_VERSION) or ESTIMATOR_APPLICATION_VERSION)
    for confidence_name in ("identification_confidence", "portion_confidence", "macro_confidence"):
        confidence_value = result.get(confidence_name)
        if confidence_value is not None:
            confidence_value = float(confidence_value)
            if not 0 <= confidence_value <= 1:
                raise ValueError(f"{confidence_name} must be within [0,1]")
            result[confidence_name] = confidence_value
    result["item_breakdown_complete"] = result.get("item_breakdown_complete", True)
    for key in ("total_best", "total_low", "total_high"):
        total = dict(result[key])
        total["calories"] = int(round(float(total["calories"])))
        total["protein_g"] = round(float(total["protein_g"]), 1)
        total["carbs_g"] = round(float(total["carbs_g"]), 1)
        total["fat_g"] = round(float(total["fat_g"]), 1)
        result[key] = total
    result["items"] = [
        {
            "name": str(item["name"]),
            "portion_g": round(float(item["portion_g"]), 1),
            "assumptions": str(item["assumptions"]),
            "assumption_categories": {
                key: str(value)
                for key, value in (item.get("assumption_categories") if isinstance(item.get("assumption_categories"), dict) else {}).items()
                if key in ASSUMPTION_KEYS and value not in (None, "")
            },
            "calories": int(round(float(item["calories"]))),
            "protein_g": round(float(item["protein_g"]), 1),
            "carbs_g": round(float(item["carbs_g"]), 1),
            "fat_g": round(float(item["fat_g"]), 1),
            "portion_low_g": round(float(item["portion_low_g"]), 1) if item.get("portion_low_g") is not None else None,
            "portion_high_g": round(float(item["portion_high_g"]), 1) if item.get("portion_high_g") is not None else None,
            "identification_confidence": float(item["identification_confidence"]) if item.get("identification_confidence") is not None else None,
            "portion_confidence": float(item["portion_confidence"]) if item.get("portion_confidence") is not None else None,
            "evidence": str(item.get("evidence", "uncertain") or "uncertain"),
        }
        for item in result["items"]
    ]
    result["variance_drivers"] = [str(item) for item in result["variance_drivers"]]
    return _reconcile_result(result)


class DirectOpenAIEstimator:
    def __init__(
        self,
        *,
        client: Any = None,
        key_cache: Optional[SSMParameterCache] = None,
        model: str = MODEL,
        vision_detail: str = VISION_DETAIL,
        max_side: int = VISION_MAX_SIDE,
        max_retries: int = OPENAI_APPLICATION_RETRIES,
        request_timeout_seconds: float = OPENAI_REQUEST_TIMEOUT_SECONDS,
        telemetry_callback: Optional[StageTelemetry] = None,
    ):
        self._client = client
        self._key_cache = key_cache
        self._model = model
        self._vision_detail = vision_detail
        self._max_side = max_side
        self._max_retries = max(0, int(max_retries))
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._telemetry_callback = telemetry_callback

    def _emit_stage(
        self,
        stage: str,
        duration_ms: int,
        result: str,
        attempt: Optional[int] = None,
        error_category: Optional[str] = None,
    ) -> None:
        if self._telemetry_callback is None:
            return
        try:
            self._telemetry_callback(stage, int(duration_ms), result, attempt, error_category)
        except Exception:
            # Telemetry must never change estimation behaviour.
            logger.debug("estimator_telemetry_failed", exc_info=True)

    def _client_for_call(self) -> Any:
        if self._client is not None:
            return self._client
        from openai import OpenAI

        return OpenAI(
            api_key=openai_key_from_environment(self._key_cache),
            timeout=self._request_timeout_seconds,
            max_retries=OPENAI_SDK_RETRIES,
        )

    async def estimate(self, image_bytes: bytes, caption: str = "", persona_hint: str = "") -> EstimationResult:
        if not image_bytes:
            raise DirectEstimationError("Empty image", retryable=False)
        if len(image_bytes) > MAX_UPLOAD_BYTES:
            raise DirectEstimationError("Image too large", retryable=False)
        if self._vision_detail not in ALLOWED_VISION_DETAILS:
            raise DirectEstimationError("Invalid vision detail", retryable=False)
        preprocess_started = time.monotonic()
        try:
            resized = downscale_for_vision(image_bytes, max_side=self._max_side, quality=80)
        except Exception as err:
            self._emit_stage(
                "image_preprocessing",
                round((time.monotonic() - preprocess_started) * 1000),
                "failure",
                error_category=type(err).__name__,
            )
            raise DirectEstimationError(f"Invalid image: {str(err)[:120]}", retryable=False) from err
        self._emit_stage("image_preprocessing", round((time.monotonic() - preprocess_started) * 1000), "success")
        prompt = PROMPT_TEMPLATE.format(
            caption=str(caption or "")[:1000],
            persona_hint=str(persona_hint or "none")[:1000],
            caption_facts=", ".join(extract_caption_facts(caption)) or "none",
            portion_guidelines="\n- ".join(PORTION_REFERENCE_GUIDELINES),
        )
        encoded = base64.b64encode(resized).decode("ascii")
        data_url = f"data:image/jpeg;base64,{encoded}"
        client_started = time.monotonic()
        try:
            client = self._client_for_call()
        except Exception as err:
            self._emit_stage(
                "openai_secret_resolution",
                round((time.monotonic() - client_started) * 1000),
                "failure",
                error_category=type(err).__name__,
            )
            raise DirectEstimationError("OpenAI client setup failed") from err
        self._emit_stage("openai_secret_resolution", round((time.monotonic() - client_started) * 1000), "success")
        last_error: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 2):
            self._emit_stage("openai_request", 0, "started", attempt=attempt)
            request_started = time.monotonic()
            try:
                response = await asyncio.to_thread(
                    client.responses.create,
                    model=self._model,
                    input=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": prompt},
                                {"type": "input_image", "image_url": data_url, "detail": self._vision_detail},
                            ],
                        }
                    ],
                    text={"format": {"type": "json_schema", "name": "meal_macros", "schema": MEAL_MACRO_SCHEMA, "strict": True}},
                )
            except Exception as err:
                self._emit_stage(
                    "openai_request",
                    round((time.monotonic() - request_started) * 1000),
                    "failure",
                    attempt=attempt,
                    error_category=type(err).__name__,
                )
                last_error = err
                if attempt <= self._max_retries:
                    await asyncio.sleep(0.2 * attempt)
                    continue
                break
            self._emit_stage("openai_request", round((time.monotonic() - request_started) * 1000), "success", attempt=attempt)
            validation_started = time.monotonic()
            try:
                output_text = getattr(response, "output_text", None)
                if not output_text:
                    raise ValueError("OpenAI response did not include output_text")
                validated = validate_result(json.loads(output_text))
            except (json.JSONDecodeError, ValueError) as err:
                self._emit_stage(
                    "estimate_validation",
                    round((time.monotonic() - validation_started) * 1000),
                    "failure",
                    attempt=attempt,
                    error_category=type(err).__name__,
                )
                raise DirectEstimationError(f"Invalid OpenAI estimate: {str(err)[:120]}", retryable=False) from err
            self._emit_stage("estimate_validation", round((time.monotonic() - validation_started) * 1000), "success", attempt=attempt)
            return EstimationResult(
                estimate=MealEstimate.from_api_payload(validated),
                model=self._model,
                usage={**_extract_usage(response), "estimator_version": ESTIMATOR_APPLICATION_VERSION},
            )
        raise DirectEstimationError(f"OpenAI estimation failed: {str(last_error)[:120]}")
