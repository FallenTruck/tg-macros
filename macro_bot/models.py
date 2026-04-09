from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

QUESTIONNAIRE_VERSION = "miniapp-v1"
QUESTIONNAIRE_SEXES = {"male", "female"}
QUESTIONNAIRE_GOALS = {"lose", "maintain", "gain"}
QUESTIONNAIRE_ACTIVITY_LEVELS = {"sedentary", "light", "moderate", "active", "very_active"}


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

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "MealItemEstimate":
        required = {"name", "portion_g", "assumptions", "calories", "protein_g", "carbs_g", "fat_g"}
        missing = sorted(required - set(payload.keys()))
        if missing:
            raise ValueError(f"Missing item keys in macro response: {', '.join(missing)}")

        return cls(
            name=str(payload["name"]),
            portion_g=float(payload["portion_g"]),
            assumptions=str(payload["assumptions"]),
            calories=float(payload["calories"]),
            protein_g=float(payload["protein_g"]),
            carbs_g=float(payload["carbs_g"]),
            fat_g=float(payload["fat_g"]),
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
        )

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
        }


@dataclass(frozen=True)
class RecommendationResult:
    summary: str
    today_totals: MacroTotal
    remaining_macros: MacroTotal
    suggestions: List[RecommendedMeal]
    source: str

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
        )

    def to_payload(self) -> Dict[str, object]:
        return {
            "summary": self.summary,
            "today_totals": self.today_totals.to_payload(),
            "remaining_macros": self.remaining_macros.to_payload(),
            "suggestions": [item.to_payload() for item in self.suggestions],
            "source": self.source,
        }


@dataclass(frozen=True)
class RecommendationRequest:
    telegram_user_id: int
    profile: UserProfile
    today_totals: MacroTotal
    remaining: RemainingMacros
    recent_meals: List[str]
    candidate_foods: List[CandidateFood]

    def to_payload(self) -> Dict[str, object]:
        return {
            "telegram_user_id": self.telegram_user_id,
            "profile": self.profile.to_payload(),
            "today_totals": self.today_totals.to_payload(),
            "remaining_macros": self.remaining.to_payload(),
            "recent_meals": list(self.recent_meals),
            "candidate_foods": [item.to_payload() for item in self.candidate_foods],
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
