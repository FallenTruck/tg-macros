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
from dataclasses import dataclass
from typing import Any, Dict, Optional

from PIL import Image

from .models import MealEstimate
from .serverless_auth import SSMParameterCache, openai_key_from_environment

logger = logging.getLogger(__name__)

MODEL = "gpt-5.4"
VISION_DETAIL = "high"
VISION_MAX_SIDE = 768
MAX_UPLOAD_BYTES = 6_000_000
ALLOWED_VISION_DETAILS = {"low", "high"}

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
        "meal_name", "calories", "protein_g", "carbs_g", "fat_g", "total_best",
        "total_low", "total_high", "items", "variance_drivers", "confidence", "notes",
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
            "calories": int(round(float(item["calories"]))),
            "protein_g": round(float(item["protein_g"]), 1),
            "carbs_g": round(float(item["carbs_g"]), 1),
            "fat_g": round(float(item["fat_g"]), 1),
        }
        for item in result["items"]
    ]
    result["variance_drivers"] = [str(item) for item in result["variance_drivers"]]
    return result


class DirectOpenAIEstimator:
    def __init__(
        self,
        *,
        client: Any = None,
        key_cache: Optional[SSMParameterCache] = None,
        model: str = MODEL,
        vision_detail: str = VISION_DETAIL,
        max_side: int = VISION_MAX_SIDE,
        max_retries: int = 2,
    ):
        self._client = client
        self._key_cache = key_cache
        self._model = model
        self._vision_detail = vision_detail
        self._max_side = max_side
        self._max_retries = max_retries

    def _client_for_call(self) -> Any:
        if self._client is not None:
            return self._client
        from openai import OpenAI

        return OpenAI(api_key=openai_key_from_environment(self._key_cache))

    async def estimate(self, image_bytes: bytes, caption: str = "", persona_hint: str = "") -> EstimationResult:
        if not image_bytes:
            raise DirectEstimationError("Empty image", retryable=False)
        if len(image_bytes) > MAX_UPLOAD_BYTES:
            raise DirectEstimationError("Image too large", retryable=False)
        if self._vision_detail not in ALLOWED_VISION_DETAILS:
            raise DirectEstimationError("Invalid vision detail", retryable=False)
        try:
            resized = downscale_for_vision(image_bytes, max_side=self._max_side, quality=80)
        except Exception as err:
            raise DirectEstimationError(f"Invalid image: {str(err)[:120]}", retryable=False) from err
        prompt = PROMPT_TEMPLATE.format(
            caption=str(caption or "")[:1000],
            persona_hint=str(persona_hint or "none")[:1000],
            portion_guidelines="\n- ".join(PORTION_REFERENCE_GUIDELINES),
        )
        encoded = base64.b64encode(resized).decode("ascii")
        data_url = f"data:image/jpeg;base64,{encoded}"
        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await asyncio.to_thread(
                    self._client_for_call().responses.create,
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
                output_text = getattr(response, "output_text", None)
                if not output_text:
                    raise ValueError("OpenAI response did not include output_text")
                validated = validate_result(json.loads(output_text))
                return EstimationResult(
                    estimate=MealEstimate.from_api_payload(validated),
                    model=self._model,
                    usage=_extract_usage(response),
                )
            except (json.JSONDecodeError, ValueError) as err:
                raise DirectEstimationError(f"Invalid OpenAI estimate: {str(err)[:120]}", retryable=False) from err
            except Exception as err:
                last_error = err
                if attempt < self._max_retries:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                break
        raise DirectEstimationError(f"OpenAI estimation failed: {str(last_error)[:120]}")
