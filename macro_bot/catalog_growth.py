from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from .models import CatalogOverlapDecision, CatalogSuggestion, LoggedMealRow, MacroTotal, UserProfile
from .services import CatalogReviewClient, CatalogReviewError
from .storage import CatalogSuggestionStore, FoodCatalogStore, MealLogRepository, UserProfileStore

logger = logging.getLogger(__name__)

_NON_ALPHA_RE = re.compile(r"[^a-z\s]+")
_MULTISPACE_RE = re.compile(r"\s+")
_NUMERIC_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)?(?:g|kg|ml|l|pc|pcs|piece|pieces|x)?$")
_STOPWORDS = {
    "the",
    "and",
    "with",
    "some",
    "abit",
    "a",
    "an",
    "of",
    "for",
    "bit",
    "little",
    "only",
    "normal",
    "homemade",
}


@dataclass(frozen=True)
class CatalogGrowthStats:
    suggestion_count: int
    refreshed_user_ids: List[int]


class CatalogGrowthService:
    def __init__(
        self,
        meal_log_repository: MealLogRepository,
        profile_store: UserProfileStore,
        food_catalog_store: FoodCatalogStore,
        suggestion_store: CatalogSuggestionStore,
        catalog_review_client: Optional[CatalogReviewClient] = None,
        min_occurrences: int = 2,
    ):
        self._meal_log_repository = meal_log_repository
        self._profile_store = profile_store
        self._food_catalog_store = food_catalog_store
        self._suggestion_store = suggestion_store
        self._catalog_review_client = catalog_review_client
        self._min_occurrences = min_occurrences

    def refresh(self, telegram_user_id: Optional[int] = None) -> CatalogGrowthStats:
        final_suggestions, refreshed_user_ids = self._build_suggestions(telegram_user_id=telegram_user_id)
        self._suggestion_store.save(final_suggestions)
        return CatalogGrowthStats(
            suggestion_count=len(final_suggestions),
            refreshed_user_ids=sorted(set(refreshed_user_ids)),
        )

    async def refresh_with_review(self, telegram_user_id: Optional[int] = None) -> CatalogGrowthStats:
        final_suggestions, refreshed_user_ids = self._build_suggestions(telegram_user_id=telegram_user_id)
        if self._catalog_review_client is not None:
            final_suggestions = await self._apply_model_overlap_review(final_suggestions)
        self._suggestion_store.save(final_suggestions)
        return CatalogGrowthStats(
            suggestion_count=len(final_suggestions),
            refreshed_user_ids=sorted(set(refreshed_user_ids)),
        )

    def _build_suggestions(self, telegram_user_id: Optional[int] = None) -> tuple[List[CatalogSuggestion], List[int]]:
        existing_suggestions = {
            item.suggestion_id: item
            for item in self._suggestion_store.list()
        }
        catalog_entries = self._food_catalog_store.list_entries()
        meals = self._meal_log_repository.list_all_meals()
        grouped: Dict[int, Dict[str, List[LoggedMealRow]]] = defaultdict(lambda: defaultdict(list))
        for meal in meals:
            if telegram_user_id is not None and meal.telegram_user_id != telegram_user_id:
                continue
            cluster_key = self._cluster_key(meal.caption)
            if not cluster_key:
                continue
            grouped[meal.telegram_user_id][cluster_key].append(meal)

        rebuilt: List[CatalogSuggestion] = []
        refreshed_user_ids: List[int] = []
        for active_telegram_user_id, clusters in grouped.items():
            try:
                profile = self._profile_store.get(active_telegram_user_id)
            except KeyError:
                logger.warning("catalog_growth_missing_profile telegram_user_id=%s", active_telegram_user_id)
                continue

            refreshed_user_ids.append(active_telegram_user_id)
            user_catalog = [
                entry
                for entry in catalog_entries
                if not entry.eligible_telegram_user_ids
                or active_telegram_user_id in entry.eligible_telegram_user_ids
            ]
            for cluster_key, cluster_meals in clusters.items():
                if len(cluster_meals) < self._min_occurrences:
                    continue
                if self._matches_existing_catalog(cluster_key, cluster_meals, user_catalog):
                    continue
                suggestion_id = self._suggestion_id(active_telegram_user_id, cluster_key, cluster_meals)
                representative = self._representative_caption(cluster_meals)
                previous = existing_suggestions.get(suggestion_id)
                rebuilt.append(
                    self._build_suggestion(
                        suggestion_id=suggestion_id,
                        telegram_user_id=active_telegram_user_id,
                        cluster_key=cluster_key,
                        profile=profile,
                        meals=cluster_meals,
                        representative_caption=representative,
                        previous=previous,
                    )
                )

        preserved = [
            item
            for item in existing_suggestions.values()
            if item.status in {"approved", "applied", "rejected"} and item.suggestion_id not in {s.suggestion_id for s in rebuilt}
        ]
        final_suggestions = sorted(
            rebuilt + preserved,
            key=lambda item: (item.telegram_user_id, item.status, -item.occurrence_count, item.proposed_name.lower()),
        )
        return final_suggestions, refreshed_user_ids

    def apply_approved(self, suggestion_ids: Optional[Iterable[str]] = None) -> int:
        suggestions = self._suggestion_store.list()
        selected_ids = set(suggestion_ids or [])
        to_apply: List[CatalogSuggestion] = []
        updated: List[CatalogSuggestion] = []
        for item in suggestions:
            should_apply = item.status == "approved" and (
                not selected_ids or item.suggestion_id in selected_ids
            )
            if should_apply:
                to_apply.append(item)
                updated.append(
                    CatalogSuggestion(
                        suggestion_id=item.suggestion_id,
                        telegram_user_id=item.telegram_user_id,
                        cluster_key=item.cluster_key,
                        proposed_name=item.proposed_name,
                        proposed_serving=item.proposed_serving,
                        macros=item.macros,
                        tags=list(item.tags),
                        cuisines=list(item.cuisines),
                        eligible_telegram_user_ids=list(item.eligible_telegram_user_ids),
                        occurrence_count=item.occurrence_count,
                        source_captions=list(item.source_captions),
                        first_seen_iso=item.first_seen_iso,
                        last_seen_iso=item.last_seen_iso,
                        status="applied",
                        notes=item.notes,
                    )
                )
            else:
                updated.append(item)

        appended = self._food_catalog_store.append_entries([item.to_catalog_entry() for item in to_apply])
        if to_apply:
            self._suggestion_store.save(updated)
        return appended

    def _build_suggestion(
        self,
        *,
        suggestion_id: str,
        telegram_user_id: int,
        cluster_key: str,
        profile: UserProfile,
        meals: List[LoggedMealRow],
        representative_caption: str,
        previous: Optional[CatalogSuggestion],
    ) -> CatalogSuggestion:
        macros = MacroTotal(
            calories=round(sum(item.calories for item in meals) / len(meals), 1),
            protein_g=round(sum(item.protein_g for item in meals) / len(meals), 1),
            carbs_g=round(sum(item.carbs_g for item in meals) / len(meals), 1),
            fat_g=round(sum(item.fat_g for item in meals) / len(meals), 1),
        )
        source_captions = self._top_captions(meals)
        first_seen = min(item.logged_at for item in meals).isoformat(timespec="seconds")
        last_seen = max(item.logged_at for item in meals).isoformat(timespec="seconds")

        tags = self._default_tags(profile, macros)
        cuisines = self._default_cuisines(profile, representative_caption)
        proposed_name = self._proposed_name(representative_caption)
        proposed_serving = "1 logged portion"
        status = "pending_review"
        notes = ""
        eligible_telegram_user_ids = [telegram_user_id]

        if previous:
            proposed_name = previous.proposed_name or proposed_name
            proposed_serving = previous.proposed_serving or proposed_serving
            tags = list(previous.tags or tags)
            cuisines = list(previous.cuisines or cuisines)
            eligible_telegram_user_ids = list(
                previous.eligible_telegram_user_ids or eligible_telegram_user_ids
            )
            status = previous.status
            notes = previous.notes

        return CatalogSuggestion(
            suggestion_id=suggestion_id,
            telegram_user_id=telegram_user_id,
            cluster_key=cluster_key,
            proposed_name=proposed_name,
            proposed_serving=proposed_serving,
            macros=macros,
            tags=tags,
            cuisines=cuisines,
            eligible_telegram_user_ids=eligible_telegram_user_ids,
            occurrence_count=len(meals),
            source_captions=source_captions,
            first_seen_iso=first_seen,
            last_seen_iso=last_seen,
            status=status,
            notes=notes,
        )

    async def _apply_model_overlap_review(
        self,
        suggestions: List[CatalogSuggestion],
    ) -> List[CatalogSuggestion]:
        if not suggestions:
            return suggestions

        catalog_entries = self._food_catalog_store.list_entries()
        relevant_catalog = [
            entry
            for entry in catalog_entries
            if not entry.eligible_telegram_user_ids
            or any(
                user_id in entry.eligible_telegram_user_ids
                for user_id in self._suggestion_user_ids(suggestions)
            )
        ]

        try:
            decisions = await self._catalog_review_client.review_overlaps(suggestions, relevant_catalog)
        except CatalogReviewError as err:
            logger.warning("catalog_overlap_review_failed detail=%s", str(err)[:120])
            return suggestions

        by_id: Dict[str, CatalogOverlapDecision] = {item.suggestion_id: item for item in decisions}
        catalog_by_id = {entry.food_id: entry for entry in catalog_entries}
        reviewed: List[CatalogSuggestion] = []
        for suggestion in suggestions:
            decision = by_id.get(suggestion.suggestion_id)
            if not decision or decision.action == "keep":
                reviewed.append(suggestion)
                continue

            duplicate_entry = catalog_by_id.get(decision.duplicate_food_id)
            duplicate_label = (
                f"{duplicate_entry.name} ({duplicate_entry.food_id})"
                if duplicate_entry
                else decision.duplicate_food_id
            )
            logger.info(
                "catalog_suggestion_pruned_duplicate suggestion_id=%s duplicate=%s rationale=%s",
                suggestion.suggestion_id,
                duplicate_label,
                decision.rationale[:160],
            )
        return reviewed

    @staticmethod
    def _cluster_key(caption: str) -> str:
        text = (caption or "").strip().lower()
        if not text:
            return ""
        text = _NON_ALPHA_RE.sub(" ", text)
        tokens = [
            token
            for token in _MULTISPACE_RE.split(text)
            if token
            and token not in _STOPWORDS
            and not _NUMERIC_TOKEN_RE.match(token)
        ]
        return " ".join(tokens)

    def _matches_existing_catalog(
        self,
        cluster_key: str,
        meals: List[LoggedMealRow],
        catalog_entries,
    ) -> bool:
        if not cluster_key:
            return True

        cluster_tokens = set(cluster_key.split())
        for entry in catalog_entries:
            entry_key = self._cluster_key(entry.name)
            entry_tokens = set(entry_key.split())
            if not entry_tokens:
                continue
            overlap = len(cluster_tokens & entry_tokens)
            ratio = overlap / max(len(entry_tokens), 1)
            if ratio >= 0.8:
                return True

        representative = self._cluster_key(self._representative_caption(meals))
        for entry in catalog_entries:
            entry_key = self._cluster_key(entry.name)
            if representative == entry_key:
                return True
        return False

    @staticmethod
    def _representative_caption(meals: List[LoggedMealRow]) -> str:
        captions = [item.caption.strip() for item in meals if item.caption.strip()]
        if not captions:
            return ""
        counter = Counter(captions)
        return sorted(counter.items(), key=lambda item: (-item[1], len(item[0]), item[0].lower()))[0][0]

    @staticmethod
    def _top_captions(meals: List[LoggedMealRow], limit: int = 3) -> List[str]:
        counter = Counter(item.caption.strip() for item in meals if item.caption.strip())
        return [caption for caption, _ in counter.most_common(limit)]

    @staticmethod
    def _default_tags(profile: UserProfile, macros: MacroTotal) -> List[str]:
        tags = ["meal" if macros.calories >= 300 else "snack"]
        if macros.protein_g >= 25:
            tags.append("high_protein")
        if "vegetarian" in profile.restrictions:
            tags.append("vegetarian")
        return sorted(set(tags))

    @staticmethod
    def _default_cuisines(profile: UserProfile, representative_caption: str) -> List[str]:
        caption = representative_caption.lower()
        matches = []
        for cuisine in profile.preferred_cuisines:
            if cuisine.lower() in caption:
                matches.append(cuisine)
        return matches[:2]

    @staticmethod
    def _proposed_name(caption: str, max_words: int = 8) -> str:
        words = [word.strip() for word in caption.replace(",", " ").split() if word.strip()]
        if not words:
            return "Suggested Meal"
        trimmed = words[:max_words]
        return " ".join(word.capitalize() if word.islower() else word for word in trimmed)

    @staticmethod
    def _suggestion_id(telegram_user_id: int, cluster_key: str, meals: List[LoggedMealRow]) -> str:
        representative = CatalogGrowthService._representative_caption(meals)
        base = re.sub(r"[^a-z0-9]+", "_", representative.lower()).strip("_")[:24] or "meal"
        digest = hashlib.sha1(f"{telegram_user_id}:{cluster_key}".encode("utf-8")).hexdigest()[:8]
        return f"{telegram_user_id}_{base}_{digest}"

    @staticmethod
    def _suggestion_user_ids(suggestions: List[CatalogSuggestion]) -> List[int]:
        return sorted({item.telegram_user_id for item in suggestions})
