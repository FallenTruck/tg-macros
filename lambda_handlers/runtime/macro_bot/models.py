from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Dict, List, Optional

QUESTIONNAIRE_VERSION = "miniapp-v2"
QUESTIONNAIRE_SEXES = {"male", "female"}
QUESTIONNAIRE_GOALS = {"lose", "maintain", "gain"}
QUESTIONNAIRE_ACTIVITY_LEVELS = {"sedentary", "light", "moderate", "active", "very_active"}
ITEM_EVIDENCE_LEVELS = {"clearly_visible", "probably_visible", "partially_occluded", "inferred", "uncertain"}
ASSUMPTION_KEYS = ("food_identity", "portion", "cooking_method", "oil_fat", "sauce_dressing", "hidden_ingredients")
ESTIMATOR_VERSION = "nutrition-estimator-v2"


@dataclass(frozen=True)
class MacroTotal:
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "MacroTotal":
        return cls(
            calories=float(payload["calories"]),
            protein_g=float(payload["protein_g"]),
            carbs_g=float(payload["carbs_g"]),
            fat_g=float(payload["fat_g"]),
        )

    @classmethod
    def zeros(cls) -> "MacroTotal":
        return cls(calories=0.0, protein_g=0.0, carbs_g=0.0, fat_g=0.0)

    def scaled(self, factor: float) -> "MacroTotal":
        return MacroTotal(
            calories=self.calories * factor,
            protein_g=self.protein_g * factor,
            carbs_g=self.carbs_g * factor,
            fat_g=self.fat_g * factor,
        )

    def clamp_non_negative(self) -> "MacroTotal":
        return MacroTotal(
            calories=max(0.0, self.calories),
            protein_g=max(0.0, self.protein_g),
            carbs_g=max(0.0, self.carbs_g),
            fat_g=max(0.0, self.fat_g),
        )

    def subtract(self, other: "MacroTotal") -> "MacroTotal":
        return MacroTotal(
            calories=self.calories - other.calories,
            protein_g=self.protein_g - other.protein_g,
            carbs_g=self.carbs_g - other.carbs_g,
            fat_g=self.fat_g - other.fat_g,
        )

    def to_payload(self) -> Dict[str, float]:
        return {
            "calories": round(self.calories, 1),
            "protein_g": round(self.protein_g, 1),
            "carbs_g": round(self.carbs_g, 1),
            "fat_g": round(self.fat_g, 1),
        }


@dataclass(frozen=True)
class MealItemEstimate:
    name: str
    portion_g: float
    assumptions: str
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    portion_low_g: Optional[float] = None
    portion_high_g: Optional[float] = None
    identification_confidence: Optional[float] = None
    portion_confidence: Optional[float] = None
    evidence: str = "uncertain"
    assumption_categories: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "MealItemEstimate":
        required = {"name", "portion_g", "assumptions", "calories", "protein_g", "carbs_g", "fat_g"}
        missing = sorted(required - set(payload.keys()))
        if missing:
            raise ValueError(f"Missing item keys in macro response: {', '.join(missing)}")

        evidence = str(payload.get("evidence", "uncertain") or "uncertain")
        if evidence not in ITEM_EVIDENCE_LEVELS:
            raise ValueError(f"Invalid item evidence level: {evidence}")

        def optional_confidence(name: str) -> Optional[float]:
            value = payload.get(name)
            if value is None or value == "":
                return None
            confidence = float(value)
            if confidence < 0 or confidence > 1:
                raise ValueError(f"{name} must be within [0,1]")
            return confidence

        portion_low = payload.get("portion_low_g")
        portion_high = payload.get("portion_high_g")
        portion = float(payload["portion_g"])
        portion_low_value = float(portion_low) if portion_low is not None else None
        portion_high_value = float(portion_high) if portion_high is not None else None
        if portion_low_value is not None and portion_high_value is not None:
            if portion_low_value > portion_high_value or not (portion_low_value <= portion <= portion_high_value):
                raise ValueError("portion_g must be within portion_low_g and portion_high_g")

        assumption_payload = payload.get("assumption_categories")
        if not isinstance(assumption_payload, dict):
            assumption_payload = {}

        return cls(
            name=str(payload["name"]),
            portion_g=portion,
            assumptions=str(payload["assumptions"]),
            calories=float(payload["calories"]),
            protein_g=float(payload["protein_g"]),
            carbs_g=float(payload["carbs_g"]),
            fat_g=float(payload["fat_g"]),
            portion_low_g=portion_low_value,
            portion_high_g=portion_high_value,
            identification_confidence=optional_confidence("identification_confidence"),
            portion_confidence=optional_confidence("portion_confidence"),
            evidence=evidence,
            assumption_categories={
                key: str(value)
                for key, value in assumption_payload.items()
                if key in ASSUMPTION_KEYS and value not in (None, "")
            },
        )

    def scaled(self, factor: float) -> "MealItemEstimate":
        return MealItemEstimate(
            name=self.name,
            portion_g=self.portion_g * factor,
            assumptions=self.assumptions,
            calories=self.calories * factor,
            protein_g=self.protein_g * factor,
            carbs_g=self.carbs_g * factor,
            fat_g=self.fat_g * factor,
            portion_low_g=self.portion_low_g * factor if self.portion_low_g is not None else None,
            portion_high_g=self.portion_high_g * factor if self.portion_high_g is not None else None,
            identification_confidence=self.identification_confidence,
            portion_confidence=self.portion_confidence,
            evidence=self.evidence,
            assumption_categories=dict(self.assumption_categories),
        )


@dataclass(frozen=True)
class MealEstimate:
    meal_name: str
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    confidence: float
    notes: str
    items: List[MealItemEstimate] = field(default_factory=list)
    total_low: Optional[MacroTotal] = None
    total_high: Optional[MacroTotal] = None
    variance_drivers: List[str] = field(default_factory=list)
    metrics_event_id: Optional[str] = None
    identification_confidence: Optional[float] = None
    portion_confidence: Optional[float] = None
    macro_confidence: Optional[float] = None
    item_breakdown_complete: bool = True
    model_reported_total: Optional[MacroTotal] = None
    item_derived_total: Optional[MacroTotal] = None
    reconciliation_status: str = "not_evaluated"
    follow_up_question: Optional[str] = None
    estimator_version: str = ESTIMATOR_VERSION

    @property
    def total_best(self) -> MacroTotal:
        return MacroTotal(
            calories=self.calories,
            protein_g=self.protein_g,
            carbs_g=self.carbs_g,
            fat_g=self.fat_g,
        )

    @classmethod
    def from_api_payload(cls, payload: Dict[str, object]) -> "MealEstimate":
        required = {
            "meal_name",
            "calories",
            "protein_g",
            "carbs_g",
            "fat_g",
            "confidence",
            "notes",
        }
        missing = sorted(required - set(payload.keys()))
        if missing:
            raise ValueError(f"Missing keys in macro response: {', '.join(missing)}")

        confidence = float(payload["confidence"])
        if confidence < 0 or confidence > 1:
            raise ValueError("Confidence must be within [0,1]")

        items_payload = payload.get("items") or []
        if not isinstance(items_payload, list):
            raise ValueError("items must be a list")
        items = [MealItemEstimate.from_payload(item) for item in items_payload]

        total_low_payload = payload.get("total_low")
        total_high_payload = payload.get("total_high")
        total_low = MacroTotal.from_payload(total_low_payload) if isinstance(total_low_payload, dict) else None
        total_high = MacroTotal.from_payload(total_high_payload) if isinstance(total_high_payload, dict) else None

        variance_drivers_payload = payload.get("variance_drivers") or []
        if not isinstance(variance_drivers_payload, list):
            raise ValueError("variance_drivers must be a list")
        variance_drivers = [str(item) for item in variance_drivers_payload]

        def optional_confidence(name: str) -> Optional[float]:
            value = payload.get(name)
            if value is None or value == "":
                return None
            confidence_value = float(value)
            if confidence_value < 0 or confidence_value > 1:
                raise ValueError(f"{name} must be within [0,1]")
            return confidence_value

        model_reported_payload = payload.get("model_reported_total")
        item_derived_payload = payload.get("item_derived_total")
        breakdown_complete = payload.get("item_breakdown_complete", True)
        if not isinstance(breakdown_complete, bool):
            raise ValueError("item_breakdown_complete must be boolean")

        return cls(
            meal_name=str(payload["meal_name"]),
            calories=float(payload["calories"]),
            protein_g=float(payload["protein_g"]),
            carbs_g=float(payload["carbs_g"]),
            fat_g=float(payload["fat_g"]),
            confidence=confidence,
            notes=str(payload.get("notes", "")),
            items=items,
            total_low=total_low,
            total_high=total_high,
            variance_drivers=variance_drivers,
            metrics_event_id=str(payload.get("metrics_event_id", "") or "") or None,
            identification_confidence=optional_confidence("identification_confidence"),
            portion_confidence=optional_confidence("portion_confidence"),
            macro_confidence=optional_confidence("macro_confidence"),
            item_breakdown_complete=breakdown_complete,
            model_reported_total=(MacroTotal.from_payload(model_reported_payload) if isinstance(model_reported_payload, dict) else None),
            item_derived_total=(MacroTotal.from_payload(item_derived_payload) if isinstance(item_derived_payload, dict) else None),
            reconciliation_status=str(payload.get("reconciliation_status", "not_evaluated") or "not_evaluated"),
            follow_up_question=str(payload.get("follow_up_question", "") or "") or None,
            estimator_version=str(payload.get("estimator_version", ESTIMATOR_VERSION) or ESTIMATOR_VERSION),
        )

    def scaled(self, factor: float) -> "MealEstimate":
        return MealEstimate(
            meal_name=self.meal_name,
            calories=self.calories * factor,
            protein_g=self.protein_g * factor,
            carbs_g=self.carbs_g * factor,
            fat_g=self.fat_g * factor,
            confidence=self.confidence,
            notes=self.notes,
            items=[item.scaled(factor) for item in self.items],
            total_low=self.total_low.scaled(factor) if self.total_low else None,
            total_high=self.total_high.scaled(factor) if self.total_high else None,
            variance_drivers=list(self.variance_drivers),
            metrics_event_id=self.metrics_event_id,
            identification_confidence=self.identification_confidence,
            portion_confidence=self.portion_confidence,
            macro_confidence=self.macro_confidence,
            item_breakdown_complete=self.item_breakdown_complete,
            model_reported_total=self.model_reported_total.scaled(factor) if self.model_reported_total else None,
            item_derived_total=self.item_derived_total.scaled(factor) if self.item_derived_total else None,
            reconciliation_status=self.reconciliation_status,
            follow_up_question=self.follow_up_question,
            estimator_version=self.estimator_version,
        )

    def to_payload(self) -> Dict[str, object]:
        return {
            "meal_name": self.meal_name,
            "calories": round(self.calories, 1),
            "protein_g": round(self.protein_g, 1),
            "carbs_g": round(self.carbs_g, 1),
            "fat_g": round(self.fat_g, 1),
            "confidence": round(self.confidence, 3),
            "notes": self.notes,
            "identification_confidence": self.identification_confidence,
            "portion_confidence": self.portion_confidence,
            "macro_confidence": self.macro_confidence,
            "items": [
                {
                    "name": item.name,
                    "portion_g": round(item.portion_g, 1),
                    "assumptions": item.assumptions,
                    "assumption_categories": dict(item.assumption_categories),
                    "calories": round(item.calories, 1),
                    "protein_g": round(item.protein_g, 1),
                    "carbs_g": round(item.carbs_g, 1),
                    "fat_g": round(item.fat_g, 1),
                    "portion_low_g": item.portion_low_g,
                    "portion_high_g": item.portion_high_g,
                    "identification_confidence": item.identification_confidence,
                    "portion_confidence": item.portion_confidence,
                    "evidence": item.evidence,
                }
                for item in self.items
            ],
            "total_best": self.total_best.to_payload(),
            "total_low": self.total_low.to_payload() if self.total_low else None,
            "total_high": self.total_high.to_payload() if self.total_high else None,
            "variance_drivers": list(self.variance_drivers),
            "item_breakdown_complete": self.item_breakdown_complete,
            "model_reported_total": self.model_reported_total.to_payload() if self.model_reported_total else None,
            "item_derived_total": self.item_derived_total.to_payload() if self.item_derived_total else None,
            "reconciliation_status": self.reconciliation_status,
            "metrics_event_id": self.metrics_event_id,
            "estimator_version": self.estimator_version,
            "follow_up_question": self.follow_up_question,
        }

    def adjust_category(self, category: str, value: str) -> "MealEstimate":
        """Apply a documented, deterministic correction to matching items."""

        category = str(category).strip().lower()
        value = str(value).strip().lower()
        if category == "portion":
            return self.scaled({"smaller": 0.8, "larger": 1.2}.get(value, 1.0))

        terms = {
            "base": ("rice", "noodle", "pasta", "bread", "roti", "chapati", "potato", "grain"),
            "protein": ("chicken", "fish", "salmon", "meat", "egg", "paneer", "tofu", "dal", "lentil"),
            "sauce": ("sauce", "gravy", "dressing", "oil", "mayo", "cheese", "curry"),
            "skin": ("chicken", "duck", "skin"),
        }
        matching = [item for item in self.items if any(term in item.name.lower() for term in terms.get(category, ()))]
        if not matching:
            return self

        def transform(item: MealItemEstimate) -> MealItemEstimate:
            if category == "skin" and value == "removed":
                new_fat = item.fat_g * 0.75
                new_calories = max(0.0, item.calories - (item.fat_g - new_fat) * 9.0)
                return replace(item, fat_g=new_fat, calories=new_calories, assumptions=f"{item.assumptions}; skin removed")
            factor = {"half": 0.5, "less": 0.7, "light": 0.5, "moderate": 1.0, "heavy": 1.5, "more": 1.3}.get(value, 1.0)
            return item.scaled(factor)

        updated = [transform(item) if item in matching else item for item in self.items]
        total = MacroTotal(
            calories=sum(item.calories for item in updated),
            protein_g=sum(item.protein_g for item in updated),
            carbs_g=sum(item.carbs_g for item in updated),
            fat_g=sum(item.fat_g for item in updated),
        )
        low = self.total_low or self.total_best
        high = self.total_high or self.total_best
        adjusted_low = MacroTotal(
            calories=min(low.calories, total.calories), protein_g=min(low.protein_g, total.protein_g),
            carbs_g=min(low.carbs_g, total.carbs_g), fat_g=min(low.fat_g, total.fat_g),
        )
        adjusted_high = MacroTotal(
            calories=max(high.calories, total.calories), protein_g=max(high.protein_g, total.protein_g),
            carbs_g=max(high.carbs_g, total.carbs_g), fat_g=max(high.fat_g, total.fat_g),
        )
        return replace(self, calories=total.calories, protein_g=total.protein_g, carbs_g=total.carbs_g, fat_g=total.fat_g, items=updated, total_low=adjusted_low, total_high=adjusted_high)

    def assumptions_summary(self, max_chars: int = 120) -> str:
        fragments: List[str] = []
        for item in self.items[:2]:
            assumption = item.assumptions.strip()
            if assumption:
                fragments.append(assumption)
        if not fragments and self.variance_drivers:
            fragments = self.variance_drivers[:2]
        if not fragments:
            fragments = [self.notes]

        text = " | ".join([frag for frag in fragments if frag]).strip()
        if not text:
            return "Standard portion and cooking assumptions applied."
        if len(text) <= max_chars:
            return text
        return f"{text[: max_chars - 3].rstrip()}..."

    def assumptions_detail(self, max_lines: int = 3) -> List[str]:
        labels = {
            "food_identity": "food identity",
            "portion": "portion",
            "cooking_method": "cooking method",
            "oil_fat": "oil/fat",
            "sauce_dressing": "sauce/dressing",
            "hidden_ingredients": "hidden ingredients",
        }
        lines: List[str] = []
        for item in self.items:
            for key, value in item.assumption_categories.items():
                value = str(value).strip()
                if value:
                    lines.append(f"{labels.get(key, key)}: {value}")
                    if len(lines) >= max_lines:
                        return lines
        if not lines:
            return [self.assumptions_summary()]
        return lines


@dataclass
class PendingMealAction:
    token: str
    chat_id: int
    request_message_id: int
    telegram_user_id: int
    username: Optional[str]
    caption: str
    estimate: MealEstimate
    status: str = "pending"
    datetime_iso: Optional[str] = None
    message_id: Optional[int] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    adjustment_factor: float = 1.0
    metrics_event_id: Optional[str] = None
    meal_id: Optional[str] = None
    canonical_sk: Optional[str] = None
    expires_at: int = 0
    original_estimate: Optional[MealEstimate] = None
    correction_state: str = ""

    def scale(self, factor: float) -> None:
        if self.status != "pending":
            raise ValueError("Cannot scale finalized meal")
        self.adjustment_factor *= factor
        self.estimate = self.estimate.scaled(factor)

    def confirm(self) -> None:
        if self.status == "confirmed":
            raise ValueError("Already logged")
        if self.status == "cancelled":
            raise ValueError("Already finalized.")
        self.status = "confirmed"

    def cancel(self) -> None:
        if self.status in {"confirmed", "cancelled"}:
            raise ValueError("Already finalized.")
        self.status = "cancelled"


@dataclass(frozen=True)
class LoggedMealRow:
    datetime_iso: str
    telegram_user_id: int
    username: str
    person: str
    caption: str
    calories: int
    protein_g: float
    carbs_g: float
    fat_g: float
    confidence: float
    message_id: int

    @classmethod
    def from_pending(cls, action: PendingMealAction, person: str) -> "LoggedMealRow":
        timestamp = action.datetime_iso or datetime.now().isoformat(timespec="seconds")
        return cls(
            datetime_iso=timestamp,
            telegram_user_id=action.telegram_user_id,
            username=action.username or "",
            person=person,
            caption=action.caption,
            calories=int(round(action.estimate.calories)),
            protein_g=round(action.estimate.protein_g, 1),
            carbs_g=round(action.estimate.carbs_g, 1),
            fat_g=round(action.estimate.fat_g, 1),
            confidence=round(float(action.estimate.confidence), 3),
            message_id=action.message_id or 0,
        )

    @classmethod
    def from_csv_row(cls, row: Dict[str, str]) -> "LoggedMealRow":
        return cls(
            datetime_iso=row["datetime"],
            telegram_user_id=int(row["telegram_user_id"]),
            username=row.get("username", ""),
            person=row.get("person", "unknown"),
            caption=row.get("caption", ""),
            calories=int(float(row.get("calories", "0") or 0)),
            protein_g=float(row.get("protein_g", "0") or 0),
            carbs_g=float(row.get("carbs_g", "0") or 0),
            fat_g=float(row.get("fat_g", "0") or 0),
            confidence=float(row.get("confidence", "0") or 0),
            message_id=int(float(row.get("message_id", "0") or 0)),
        )

    @property
    def consumed_macros(self) -> MacroTotal:
        return MacroTotal(
            calories=float(self.calories),
            protein_g=self.protein_g,
            carbs_g=self.carbs_g,
            fat_g=self.fat_g,
        )

    @property
    def logged_at(self) -> datetime:
        return datetime.fromisoformat(self.datetime_iso)

    def to_csv_row(self) -> Dict[str, object]:
        return {
            "datetime": self.datetime_iso,
            "telegram_user_id": self.telegram_user_id,
            "username": self.username,
            "person": self.person,
            "caption": self.caption,
            "calories": self.calories,
            "protein_g": f"{self.protein_g:.1f}",
            "carbs_g": f"{self.carbs_g:.1f}",
            "fat_g": f"{self.fat_g:.1f}",
            "confidence": f"{self.confidence:.3f}",
            "message_id": self.message_id,
        }


@dataclass(frozen=True)
class UserProfile:
    telegram_user_id: int
    username: str
    display_name: str
    daily_target: MacroTotal
    questionnaire_answers: Optional["QuestionnaireAnswers"] = None
    questionnaire_version: str = QUESTIONNAIRE_VERSION
    updated_at: str = ""
    timezone: str = "Asia/Singapore"
    created_at: str = ""
    dietary_preferences: List[str] = field(default_factory=list)
    restrictions: List[str] = field(default_factory=list)
    preferred_cuisines: List[str] = field(default_factory=list)
    preferred_staples: List[str] = field(default_factory=list)
    preferred_tags: List[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "UserProfile":
        required = {"telegram_user_id", "display_name", "daily_target"}
        missing = sorted(required - set(payload.keys()))
        if missing:
            raise ValueError(f"Missing profile keys: {', '.join(missing)}")

        questionnaire_payload = payload.get("questionnaire_answers")
        return cls(
            telegram_user_id=int(payload["telegram_user_id"]),
            username=str(payload.get("username", "") or ""),
            display_name=str(payload["display_name"]),
            daily_target=MacroTotal.from_payload(payload["daily_target"]),
            questionnaire_answers=(
                QuestionnaireAnswers.from_payload(questionnaire_payload)
                if isinstance(questionnaire_payload, dict)
                else None
            ),
            questionnaire_version=str(payload.get("questionnaire_version", QUESTIONNAIRE_VERSION)),
            updated_at=str(payload.get("updated_at", "") or ""),
            timezone=str(payload.get("timezone", "Asia/Singapore") or "Asia/Singapore"),
            created_at=str(payload.get("created_at", "") or ""),
            dietary_preferences=[str(x) for x in payload.get("dietary_preferences", [])],
            restrictions=[str(x) for x in payload.get("restrictions", [])],
            preferred_cuisines=[str(x) for x in payload.get("preferred_cuisines", [])],
            preferred_staples=[str(x) for x in payload.get("preferred_staples", [])],
            preferred_tags=[str(x) for x in payload.get("preferred_tags", [])],
        )

    def to_payload(self) -> Dict[str, object]:
        return {
            "telegram_user_id": self.telegram_user_id,
            "username": self.username,
            "display_name": self.display_name,
            "daily_target": self.daily_target.to_payload(),
            "questionnaire_answers": (
                self.questionnaire_answers.to_payload()
                if self.questionnaire_answers is not None
                else None
            ),
            "questionnaire_version": self.questionnaire_version,
            "updated_at": self.updated_at,
            "timezone": self.timezone,
            "created_at": self.created_at,
            "dietary_preferences": list(self.dietary_preferences),
            "restrictions": list(self.restrictions),
            "preferred_cuisines": list(self.preferred_cuisines),
            "preferred_staples": list(self.preferred_staples),
            "preferred_tags": list(self.preferred_tags),
        }


@dataclass(frozen=True)
class FoodCatalogEntry:
    food_id: str
    name: str
    serving: str
    macros: MacroTotal
    tags: List[str] = field(default_factory=list)
    cuisines: List[str] = field(default_factory=list)
    eligible_telegram_user_ids: List[int] = field(default_factory=list)
    meal_type: str = "full_meal"
    contains: List[str] = field(default_factory=list)
    available: bool = True
    nutrition_source: str = "Legacy curated serving estimate; recipe-dependent."

    def __post_init__(self):
        if self.meal_type not in {"full_meal", "light_meal", "snack", "protein_top_up"}:
            raise ValueError("Unknown catalogue meal type")

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "FoodCatalogEntry":
        required = {"food_id", "name", "serving", "macros"}
        missing = sorted(required - set(payload.keys()))
        if missing:
            raise ValueError(f"Missing food catalog keys: {', '.join(missing)}")

        eligible_ids_payload = payload.get("eligible_telegram_user_ids")
        if eligible_ids_payload is None:
            eligible_ids_payload = payload.get("people", [])
        return cls(
            food_id=str(payload["food_id"]),
            name=str(payload["name"]),
            serving=str(payload["serving"]),
            macros=MacroTotal.from_payload(payload["macros"]),
            tags=[str(x) for x in payload.get("tags", [])],
            cuisines=[str(x) for x in payload.get("cuisines", [])],
            eligible_telegram_user_ids=[int(x) for x in eligible_ids_payload or []],
            meal_type=str(payload.get("meal_type", "full_meal")),
            contains=[str(x) for x in payload.get("contains", [])],
            available=payload.get("available", True) is True,
            nutrition_source=str(payload.get("nutrition_source", "Legacy curated serving estimate; recipe-dependent.")),
        )

    def to_payload(self) -> Dict[str, object]:
        return {
            "food_id": self.food_id,
            "name": self.name,
            "serving": self.serving,
            "macros": self.macros.to_payload(),
            "tags": list(self.tags),
            "cuisines": list(self.cuisines),
            "eligible_telegram_user_ids": list(self.eligible_telegram_user_ids),
            "meal_type": self.meal_type,
            "contains": list(self.contains),
            "available": self.available,
            "nutrition_source": self.nutrition_source,
        }


@dataclass(frozen=True)
class CandidateFood:
    food_id: str
    name: str
    serving: str
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    tags: List[str] = field(default_factory=list)
    cuisines: List[str] = field(default_factory=list)
    fit_score: float = 0.0
    fit_reason: str = ""
    meal_type: str = "full_meal"

    @classmethod
    def from_catalog(
        cls,
        entry: FoodCatalogEntry,
        fit_score: float = 0.0,
        fit_reason: str = "",
    ) -> "CandidateFood":
        return cls(
            food_id=entry.food_id,
            name=entry.name,
            serving=entry.serving,
            calories=entry.macros.calories,
            protein_g=entry.macros.protein_g,
            carbs_g=entry.macros.carbs_g,
            fat_g=entry.macros.fat_g,
            tags=list(entry.tags),
            cuisines=list(entry.cuisines),
            fit_score=fit_score,
            fit_reason=fit_reason,
            meal_type=entry.meal_type,
        )

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "CandidateFood":
        required = {
            "food_id",
            "name",
            "serving",
            "calories",
            "protein_g",
            "carbs_g",
            "fat_g",
        }
        missing = sorted(required - set(payload.keys()))
        if missing:
            raise ValueError(f"Missing candidate food keys: {', '.join(missing)}")
        return cls(
            food_id=str(payload["food_id"]),
            name=str(payload["name"]),
            serving=str(payload["serving"]),
            calories=float(payload["calories"]),
            protein_g=float(payload["protein_g"]),
            carbs_g=float(payload["carbs_g"]),
            fat_g=float(payload["fat_g"]),
            tags=[str(x) for x in payload.get("tags", [])],
            cuisines=[str(x) for x in payload.get("cuisines", [])],
            fit_score=float(payload.get("fit_score", 0.0) or 0.0),
            fit_reason=str(payload.get("fit_reason", "")),
            meal_type=str(payload.get("meal_type", "full_meal")),
        )

    @property
    def macros(self) -> MacroTotal:
        return MacroTotal(
            calories=self.calories,
            protein_g=self.protein_g,
            carbs_g=self.carbs_g,
            fat_g=self.fat_g,
        )

    def to_payload(self) -> Dict[str, object]:
        return {
            "food_id": self.food_id,
            "name": self.name,
            "serving": self.serving,
            "calories": round(self.calories, 1),
            "protein_g": round(self.protein_g, 1),
            "carbs_g": round(self.carbs_g, 1),
            "fat_g": round(self.fat_g, 1),
            "tags": list(self.tags),
            "cuisines": list(self.cuisines),
            "fit_score": round(self.fit_score, 3),
            "fit_reason": self.fit_reason,
            "meal_type": self.meal_type,
        }


@dataclass(frozen=True)
class DailyMacroSummary:
    telegram_user_id: int
    date_iso: str
    totals: MacroTotal
    meals: List[LoggedMealRow] = field(default_factory=list)

    @property
    def meal_count(self) -> int:
        return len(self.meals)

    @property
    def recent_captions(self) -> List[str]:
        return [meal.caption for meal in self.meals]


@dataclass(frozen=True)
class RemainingMacros:
    target: MacroTotal
    consumed: MacroTotal
    remaining_raw: MacroTotal
    remaining: MacroTotal
    over_calories: bool
    over_protein: bool
    over_carbs: bool
    over_fat: bool

    @classmethod
    def from_target_and_consumed(cls, target: MacroTotal, consumed: MacroTotal) -> "RemainingMacros":
        raw_remaining = target.subtract(consumed)
        return cls(
            target=target,
            consumed=consumed,
            remaining_raw=raw_remaining,
            remaining=raw_remaining.clamp_non_negative(),
            over_calories=raw_remaining.calories < 0,
            over_protein=raw_remaining.protein_g < 0,
            over_carbs=raw_remaining.carbs_g < 0,
            over_fat=raw_remaining.fat_g < 0,
        )

    @property
    def all_major_macros_exceeded(self) -> bool:
        return self.over_protein and self.over_carbs and self.over_fat

    def should_suggest(self, minimum_calories: float = 200.0) -> bool:
        return self.remaining.calories >= minimum_calories and not self.all_major_macros_exceeded

    def to_payload(self) -> Dict[str, object]:
        return {
            "target": self.target.to_payload(),
            "consumed": self.consumed.to_payload(),
            "remaining_raw": self.remaining_raw.to_payload(),
            "remaining": self.remaining.to_payload(),
            "over_calories": self.over_calories,
            "over_protein": self.over_protein,
            "over_carbs": self.over_carbs,
            "over_fat": self.over_fat,
        }


@dataclass(frozen=True)
class RecommendedMeal:
    name: str
    serving: str
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    fit_rationale: str
    tradeoffs: str
    candidate_id: str = ""

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "RecommendedMeal":
        required = {
            "name",
            "serving",
            "calories",
            "protein_g",
            "carbs_g",
            "fat_g",
            "fit_rationale",
            "tradeoffs",
        }
        missing = sorted(required - set(payload.keys()))
        if missing:
            raise ValueError(f"Missing recommendation keys: {', '.join(missing)}")

        return cls(
            name=str(payload["name"]),
            serving=str(payload["serving"]),
            calories=float(payload["calories"]),
            protein_g=float(payload["protein_g"]),
            carbs_g=float(payload["carbs_g"]),
            fat_g=float(payload["fat_g"]),
            fit_rationale=str(payload["fit_rationale"]),
            tradeoffs=str(payload["tradeoffs"]),
            candidate_id=str(payload.get("candidate_id", "") or ""),
        )

    @classmethod
    def from_candidate(cls, candidate: CandidateFood, fit_rationale: str, tradeoffs: str) -> "RecommendedMeal":
        return cls(
            name=candidate.name,
            serving=candidate.serving,
            calories=candidate.calories,
            protein_g=candidate.protein_g,
            carbs_g=candidate.carbs_g,
            fat_g=candidate.fat_g,
            fit_rationale=fit_rationale,
            tradeoffs=tradeoffs,
            candidate_id=candidate.food_id,
        )

    def to_payload(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "serving": self.serving,
            "calories": round(self.calories, 1),
            "protein_g": round(self.protein_g, 1),
            "carbs_g": round(self.carbs_g, 1),
            "fat_g": round(self.fat_g, 1),
            "fit_rationale": self.fit_rationale,
            "tradeoffs": self.tradeoffs,
            "candidate_id": self.candidate_id,
        }


@dataclass(frozen=True)
class RecommendationResult:
    summary: str
    today_totals: MacroTotal
    remaining_macros: MacroTotal
    suggestions: List[RecommendedMeal]
    source: str
    strategy_version: str = "nutrition-recommendation-v3"

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "RecommendationResult":
        required = {"summary", "today_totals", "remaining_macros", "suggestions", "source"}
        missing = sorted(required - set(payload.keys()))
        if missing:
            raise ValueError(f"Missing recommendation result keys: {', '.join(missing)}")

        suggestions_payload = payload["suggestions"]
        if not isinstance(suggestions_payload, list) or not suggestions_payload:
            raise ValueError("suggestions must be a non-empty list")

        return cls(
            summary=str(payload["summary"]),
            today_totals=MacroTotal.from_payload(payload["today_totals"]),
            remaining_macros=MacroTotal.from_payload(payload["remaining_macros"]),
            suggestions=[RecommendedMeal.from_payload(item) for item in suggestions_payload],
            source=str(payload["source"]),
            strategy_version=str(payload.get("strategy_version", "nutrition-recommendation-v2") or "nutrition-recommendation-v2"),
        )

    def to_payload(self) -> Dict[str, object]:
        return {
            "summary": self.summary,
            "today_totals": self.today_totals.to_payload(),
            "remaining_macros": self.remaining_macros.to_payload(),
            "suggestions": [item.to_payload() for item in self.suggestions],
            "source": self.source,
            "strategy_version": self.strategy_version,
        }


@dataclass(frozen=True)
class RecommendationRequest:
    telegram_user_id: int
    profile: UserProfile
    today_totals: MacroTotal
    remaining: RemainingMacros
    recent_meals: List[str]
    candidate_foods: List[CandidateFood]
    today_meals: List[Dict[str, object]] = field(default_factory=list)
    local_time: str = ""
    timing: Dict[str, object] = field(default_factory=dict)
    strategy_signal: str = ""

    def to_payload(self) -> Dict[str, object]:
        return {
            "telegram_user_id": self.telegram_user_id,
            "profile": self.profile.to_payload(),
            "today_totals": self.today_totals.to_payload(),
            "remaining_macros": self.remaining.to_payload(),
            "recent_meals": list(self.recent_meals),
            "candidate_foods": [item.to_payload() for item in self.candidate_foods],
            "today_meals": list(self.today_meals),
            "local_time": self.local_time,
            "timing": dict(self.timing),
            "strategy_signal": self.strategy_signal,
        }


@dataclass(frozen=True)
class CatalogSuggestion:
    suggestion_id: str
    telegram_user_id: int
    cluster_key: str
    proposed_name: str
    proposed_serving: str
    macros: MacroTotal
    tags: List[str] = field(default_factory=list)
    cuisines: List[str] = field(default_factory=list)
    eligible_telegram_user_ids: List[int] = field(default_factory=list)
    occurrence_count: int = 0
    source_captions: List[str] = field(default_factory=list)
    first_seen_iso: str = ""
    last_seen_iso: str = ""
    status: str = "pending_review"
    notes: str = ""

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "CatalogSuggestion":
        required = {
            "suggestion_id",
            "telegram_user_id",
            "cluster_key",
            "proposed_name",
            "proposed_serving",
            "macros",
            "occurrence_count",
            "source_captions",
            "first_seen_iso",
            "last_seen_iso",
            "status",
        }
        missing = sorted(required - set(payload.keys()))
        if missing:
            raise ValueError(f"Missing catalog suggestion keys: {', '.join(missing)}")

        eligible_ids_payload = payload.get("eligible_telegram_user_ids")
        if eligible_ids_payload is None:
            eligible_ids_payload = payload.get("people", [])
        return cls(
            suggestion_id=str(payload["suggestion_id"]),
            telegram_user_id=int(payload["telegram_user_id"]),
            cluster_key=str(payload["cluster_key"]),
            proposed_name=str(payload["proposed_name"]),
            proposed_serving=str(payload["proposed_serving"]),
            macros=MacroTotal.from_payload(payload["macros"]),
            tags=[str(x) for x in payload.get("tags", [])],
            cuisines=[str(x) for x in payload.get("cuisines", [])],
            eligible_telegram_user_ids=[int(x) for x in eligible_ids_payload or []],
            occurrence_count=int(payload["occurrence_count"]),
            source_captions=[str(x) for x in payload.get("source_captions", [])],
            first_seen_iso=str(payload["first_seen_iso"]),
            last_seen_iso=str(payload["last_seen_iso"]),
            status=str(payload.get("status", "pending_review")),
            notes=str(payload.get("notes", "")),
        )

    def to_payload(self) -> Dict[str, object]:
        return {
            "suggestion_id": self.suggestion_id,
            "telegram_user_id": self.telegram_user_id,
            "cluster_key": self.cluster_key,
            "proposed_name": self.proposed_name,
            "proposed_serving": self.proposed_serving,
            "macros": self.macros.to_payload(),
            "tags": list(self.tags),
            "cuisines": list(self.cuisines),
            "eligible_telegram_user_ids": list(self.eligible_telegram_user_ids),
            "occurrence_count": self.occurrence_count,
            "source_captions": list(self.source_captions),
            "first_seen_iso": self.first_seen_iso,
            "last_seen_iso": self.last_seen_iso,
            "status": self.status,
            "notes": self.notes,
        }

    def to_catalog_entry(self) -> FoodCatalogEntry:
        return FoodCatalogEntry(
            food_id=self.suggestion_id,
            name=self.proposed_name,
            serving=self.proposed_serving,
            macros=self.macros,
            tags=list(self.tags),
            cuisines=list(self.cuisines),
            eligible_telegram_user_ids=list(self.eligible_telegram_user_ids),
        )


@dataclass(frozen=True)
class QuestionnaireAnswers:
    sex: str
    age_years: int
    height_cm: float
    weight_kg: float
    activity_level: str
    goal: str

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "QuestionnaireAnswers":
        required = {"sex", "age_years", "height_cm", "weight_kg", "activity_level", "goal"}
        missing = sorted(required - set(payload.keys()))
        if missing:
            raise ValueError(f"Missing questionnaire keys: {', '.join(missing)}")

        sex = str(payload["sex"]).strip().lower()
        activity_level = str(payload["activity_level"]).strip().lower()
        goal = str(payload["goal"]).strip().lower()
        age_years = int(payload["age_years"])
        height_cm = float(payload["height_cm"])
        weight_kg = float(payload["weight_kg"])

        if sex not in QUESTIONNAIRE_SEXES:
            raise ValueError("sex must be male or female")
        if activity_level not in QUESTIONNAIRE_ACTIVITY_LEVELS:
            raise ValueError("activity_level is invalid")
        if goal not in QUESTIONNAIRE_GOALS:
            raise ValueError("goal must be lose, maintain, or gain")
        if age_years < 13 or age_years > 120:
            raise ValueError("age_years must be between 13 and 120")
        if height_cm < 100 or height_cm > 250:
            raise ValueError("height_cm must be between 100 and 250")
        if weight_kg < 30 or weight_kg > 350:
            raise ValueError("weight_kg must be between 30 and 350")

        return cls(
            sex=sex,
            age_years=age_years,
            height_cm=round(height_cm, 1),
            weight_kg=round(weight_kg, 1),
            activity_level=activity_level,
            goal=goal,
        )

    def to_payload(self) -> Dict[str, object]:
        return {
            "sex": self.sex,
            "age_years": self.age_years,
            "height_cm": round(self.height_cm, 1),
            "weight_kg": round(self.weight_kg, 1),
            "activity_level": self.activity_level,
            "goal": self.goal,
        }


@dataclass(frozen=True)
class CatalogOverlapDecision:
    suggestion_id: str
    action: str
    duplicate_food_id: str = ""
    rationale: str = ""

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "CatalogOverlapDecision":
        required = {"suggestion_id", "action", "duplicate_food_id", "rationale"}
        missing = sorted(required - set(payload.keys()))
        if missing:
            raise ValueError(f"Missing catalog overlap decision keys: {', '.join(missing)}")

        action = str(payload["action"])
        if action not in {"keep", "reject_duplicate"}:
            raise ValueError("Catalog overlap action must be keep or reject_duplicate")

        return cls(
            suggestion_id=str(payload["suggestion_id"]),
            action=action,
            duplicate_food_id=str(payload.get("duplicate_food_id", "") or ""),
            rationale=str(payload.get("rationale", "")),
        )
