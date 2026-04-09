from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Sequence

from .models import (
    CandidateFood,
    DailyMacroSummary,
    FoodCatalogEntry,
    MacroTotal,
    RecommendedMeal,
    RecommendationRequest,
    RecommendationResult,
    RemainingMacros,
    UserProfile,
)
from .services import RecommendationClient, RecommendationError
from .storage import FoodCatalogStore, MealLogRepository, UserProfileStore


@dataclass(frozen=True)
class PreparedRecommendation:
    profile: UserProfile
    daily_summary: DailyMacroSummary
    remaining: RemainingMacros
    recent_meals: List[str]
    candidate_foods: List[CandidateFood]

    @property
    def skip_reason(self) -> str:
        if self.remaining.remaining.calories < 200:
            return "You have less than 200 kcal remaining for today."
        if self.remaining.all_major_macros_exceeded:
            return "You are already over protein, carbs, and fat targets for today."
        if not self.candidate_foods:
            return "No suitable meal suggestions are available for your current targets."
        return ""


class RecommendationPlanner:
    def __init__(
        self,
        meal_log_repository: MealLogRepository,
        profile_store: UserProfileStore,
        food_catalog_store: FoodCatalogStore,
        recommendation_client: RecommendationClient,
    ):
        self._meal_log_repository = meal_log_repository
        self._profile_store = profile_store
        self._food_catalog_store = food_catalog_store
        self._recommendation_client = recommendation_client

    def prepare(self, telegram_user_id: int, target_date: date | None = None) -> PreparedRecommendation:
        active_date = target_date or datetime.now().date()
        profile = self._profile_store.get(telegram_user_id)
        daily_summary = self._meal_log_repository.get_daily_summary(telegram_user_id, active_date)
        remaining = RemainingMacros.from_target_and_consumed(profile.daily_target, daily_summary.totals)
        recent_logged_meals = self._meal_log_repository.list_recent_meals(telegram_user_id, limit=6)
        recent_meals = [meal.caption for meal in recent_logged_meals]
        candidate_foods = self._build_candidates(
            telegram_user_id=telegram_user_id,
            profile=profile,
            remaining=remaining,
            recent_meals=recent_meals,
        )
        return PreparedRecommendation(
            profile=profile,
            daily_summary=daily_summary,
            remaining=remaining,
            recent_meals=recent_meals,
            candidate_foods=candidate_foods,
        )

    async def recommend_next_meal(
        self,
        telegram_user_id: int,
        target_date: date | None = None,
    ) -> tuple[RecommendationResult, PreparedRecommendation]:
        prepared = self.prepare(telegram_user_id=telegram_user_id, target_date=target_date)
        if prepared.skip_reason:
            return self.build_skip_result(prepared), prepared

        request = RecommendationRequest(
            telegram_user_id=telegram_user_id,
            profile=prepared.profile,
            today_totals=prepared.daily_summary.totals,
            remaining=prepared.remaining,
            recent_meals=prepared.recent_meals,
            candidate_foods=prepared.candidate_foods,
        )
        try:
            result = await self._recommendation_client.recommend(request)
        except RecommendationError:
            result = self.build_fallback_result(prepared)
        return result, prepared

    def build_fallback_result(
        self,
        prepared: PreparedRecommendation,
        source: str = "deterministic_fallback",
    ) -> RecommendationResult:
        suggestions = [
            RecommendedMeal.from_candidate(
                candidate,
                fit_rationale=candidate.fit_reason or "Balanced fit for your remaining macros.",
                tradeoffs=self._fallback_tradeoff(candidate, prepared.remaining),
            )
            for candidate in prepared.candidate_foods[:3]
        ]
        if not suggestions:
            return self.build_skip_result(prepared)

        return RecommendationResult(
            summary=self._summary_line(prepared),
            today_totals=prepared.daily_summary.totals,
            remaining_macros=prepared.remaining.remaining,
            suggestions=suggestions,
            source=source,
        )

    @staticmethod
    def build_skip_result(prepared: PreparedRecommendation) -> RecommendationResult:
        return RecommendationResult(
            summary=prepared.skip_reason or "No recommendation needed right now.",
            today_totals=prepared.daily_summary.totals,
            remaining_macros=prepared.remaining.remaining,
            suggestions=[],
            source="skipped",
        )

    def _build_candidates(
        self,
        telegram_user_id: int,
        profile: UserProfile,
        remaining: RemainingMacros,
        recent_meals: Sequence[str],
    ) -> List[CandidateFood]:
        recent_text = " ".join(meal.lower() for meal in recent_meals)
        candidates: List[CandidateFood] = []
        for entry in self._food_catalog_store.list_entries():
            if entry.eligible_telegram_user_ids and telegram_user_id not in entry.eligible_telegram_user_ids:
                continue
            if "vegetarian" in profile.restrictions and "vegetarian" not in entry.tags:
                continue

            fit_score, fit_reason = self._score_entry(entry, profile, remaining, recent_text)
            if fit_score < -25:
                continue
            candidates.append(CandidateFood.from_catalog(entry, fit_score=fit_score, fit_reason=fit_reason))

        candidates.sort(key=lambda item: (-item.fit_score, item.name))
        return candidates[:5]

    def _score_entry(
        self,
        entry: FoodCatalogEntry,
        profile: UserProfile,
        remaining: RemainingMacros,
        recent_text: str,
    ) -> tuple[float, str]:
        desired = self._desired_next_meal_macros(remaining.remaining)
        score = 0.0
        reasons: List[str] = []

        score += self._closeness_score(entry.macros.calories, desired.calories, 220) * 28
        score += self._closeness_score(entry.macros.protein_g, desired.protein_g, 18) * 26
        score += self._closeness_score(entry.macros.carbs_g, desired.carbs_g, 28) * 20
        score += self._closeness_score(entry.macros.fat_g, desired.fat_g, 12) * 12

        if remaining.remaining.protein_g >= 30 and entry.macros.protein_g >= 25:
            score += 12
            reasons.append("strong protein fit")
        if remaining.remaining.calories > 0 and entry.macros.calories <= remaining.remaining.calories + 120:
            score += 10
            reasons.append("fits your remaining calories")
        if set(entry.cuisines) & set(profile.preferred_cuisines):
            score += 8
            reasons.append("matches your usual cuisine")
        if set(entry.tags) & set(profile.preferred_tags):
            score += 6
            reasons.append("aligns with your preferred meal style")
        if any(staple.lower() in entry.name.lower() for staple in profile.preferred_staples):
            score += 6
            reasons.append("close to foods you already eat often")

        repeat_penalty = self._repeat_penalty(entry, recent_text)
        if repeat_penalty:
            score -= repeat_penalty

        if not reasons:
            reasons.append("balanced fit for the remaining day macros")

        return score, ", ".join(reasons[:2])

    @staticmethod
    def _closeness_score(value: float, target: float, tolerance: float) -> float:
        if tolerance <= 0:
            return 0.0
        return max(0.0, 1.0 - abs(value - target) / tolerance)

    @staticmethod
    def _desired_next_meal_macros(remaining: MacroTotal) -> MacroTotal:
        return MacroTotal(
            calories=min(max(remaining.calories * 0.55, 280.0), 700.0),
            protein_g=min(max(remaining.protein_g * 0.6, 18.0), 50.0),
            carbs_g=min(max(remaining.carbs_g * 0.5, 20.0), 80.0),
            fat_g=min(max(remaining.fat_g * 0.45, 6.0), 24.0),
        )

    @staticmethod
    def _repeat_penalty(entry: FoodCatalogEntry, recent_text: str) -> float:
        tokens = [token for token in entry.name.lower().replace("-", " ").split() if len(token) > 3]
        overlap = sum(1 for token in tokens if token in recent_text)
        return float(overlap * 8)

    @staticmethod
    def _summary_line(prepared: PreparedRecommendation) -> str:
        remaining = prepared.remaining.remaining
        return (
            f"Today so far: {int(round(prepared.daily_summary.totals.calories))} kcal. "
            f"Remaining: {int(round(remaining.calories))} kcal, "
            f"{remaining.protein_g:.0f}g protein, {remaining.carbs_g:.0f}g carbs, {remaining.fat_g:.0f}g fat."
        )

    @staticmethod
    def _fallback_tradeoff(candidate: CandidateFood, remaining: RemainingMacros) -> str:
        notes: List[str] = []
        if candidate.calories > remaining.remaining.calories and remaining.remaining.calories > 0:
            notes.append("slightly heavy on calories")
        if candidate.fat_g > max(remaining.remaining.fat_g, 12):
            notes.append("fat may run a bit high")
        if candidate.protein_g < 20 and remaining.remaining.protein_g > 25:
            notes.append("protein top-up may still be needed later")
        if not notes:
            notes.append("balanced enough for the next meal slot")
        return "; ".join(notes)
