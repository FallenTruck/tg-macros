"""Nutrition application services shared by the Telegram worker and API."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from zoneinfo import ZoneInfo

from .models import MacroTotal, QuestionnaireAnswers, RemainingMacros, UserProfile
from .profile_targets import (
    ACTIVITY_LEVEL_OPTIONS,
    GOAL_OPTIONS,
    derive_daily_target,
    questionnaire_meta_payload,
)
from .recommendations import RecommendationPlanner, ServerlessRecommendationClient
from .serverless_data import (
    DEFAULT_TIMEZONE,
    ActionNotFound,
    DynamoNutritionRepository,
    ServerlessIdentity,
    StoredMeal,
    local_day_utc_bounds,
    parse_utc,
    utc_iso,
)
from .workout_programme import PROGRAMME_ID
from .workout_execution import WorkoutExecutionRepository

logger = logging.getLogger(__name__)


class InvalidUserInput(ValueError):
    """Raised for input that should be acknowledged rather than retried."""


class ReadOnlyFoodCatalogStore:
    """Read the packaged catalogue without migration or mutation."""

    def __init__(self, catalog_path: Optional[Path] = None):
        default_path = Path(__file__).resolve().parent.parent / "food_catalog.json"
        self._catalog_path = catalog_path or Path(os.getenv("FOOD_CATALOG_PATH", str(default_path)))

    def list_entries(self):
        from .models import FoodCatalogEntry

        with self._catalog_path.open("r", encoding="utf-8") as catalog_file:
            payload = json.load(catalog_file)
        foods = payload.get("foods", []) if isinstance(payload, dict) else []
        return [FoodCatalogEntry.from_payload(item) for item in foods]


class _DynamoProfileStore:
    def __init__(self, repository: DynamoNutritionRepository):
        self._repository = repository

    def get(self, telegram_user_id: int) -> UserProfile:
        identity = self._repository.resolve_identity(telegram_user_id)
        profile = self._repository.get_profile(identity.user_id)
        if profile is None:
            raise KeyError(f"Unknown profile: {telegram_user_id}")
        return profile


class _DynamoMealLogRepository:
    def __init__(self, repository: DynamoNutritionRepository):
        self._repository = repository

    def _identity(self, telegram_user_id: int) -> ServerlessIdentity:
        return self._repository.resolve_identity(telegram_user_id)

    def get_daily_summary(self, telegram_user_id: int, target_date: date):
        identity = self._identity(telegram_user_id)
        profile = self._repository.get_profile(identity.user_id)
        timezone_name = profile.timezone if profile else DEFAULT_TIMEZONE
        return self._repository.daily_summary(identity, target_date, timezone_name)

    def list_recent_meals(self, telegram_user_id: int, limit: int = 5):
        return [
            _meal_to_logged_row(meal)
            for meal in self._repository.list_recent_meals(self._identity(telegram_user_id), limit=limit)
        ]


class NutritionService:
    """Use-case boundary independent of Telegram, HTTP, and file storage."""

    def __init__(
        self,
        repository: DynamoNutritionRepository,
        *,
        catalog_store: Optional[ReadOnlyFoodCatalogStore] = None,
        now_fn: Any = None,
    ):
        self.repository = repository
        self.catalog_store = catalog_store or ReadOnlyFoodCatalogStore()
        self._now_fn = now_fn or repository.now_fn
        self._planner = RecommendationPlanner(
            _DynamoMealLogRepository(repository),
            _DynamoProfileStore(repository),
            self.catalog_store,
            recommendation_client=ServerlessRecommendationClient(),
        )
        self.workout_execution = WorkoutExecutionRepository(repository)

    def _now(self) -> datetime:
        value = self._now_fn()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def resolve_user(self, telegram_user_id: int, username: str = "", display_name: str = "") -> ServerlessIdentity:
        return self.repository.resolve_identity(telegram_user_id, username=username, display_name=display_name)

    def create_mini_app_launch(
        self,
        token: str,
        *,
        identity: ServerlessIdentity,
        chat_id: int,
        chat_type: str,
        message_id: int,
        launch_type: str = "nutrition",
        requested_day: Optional[str] = None,
    ) -> dict[str, Any]:
        return self.repository.create_mini_app_launch(
            token,
            identity=identity,
            chat_id=chat_id,
            chat_type=chat_type,
            message_id=message_id,
            launch_type=launch_type,
            requested_day=requested_day,
        )

    def profile_response(self, identity: ServerlessIdentity) -> dict[str, Any]:
        profile = self.repository.get_profile(identity.user_id)
        profile_payload = profile.to_payload() if profile is not None else None
        if profile_payload is not None:
            profile_payload["target_effective_at"] = self.current_target_effective_at(identity)
        payload: dict[str, Any] = {
            "profile": profile_payload,
            "questionnaire_version": "miniapp-v2",
            "viewer": {
                "telegram_user_id": identity.telegram_user_id,
                "username": identity.username,
                "display_name": identity.display_name,
            },
        }
        payload.update(questionnaire_meta_payload())
        if profile is not None:
            payload["timezone"] = profile.timezone
        return payload

    def current_target_effective_at(self, identity: ServerlessIdentity) -> Optional[str]:
        """Return the effective timestamp of the current target revision."""

        cutoff = self._now()
        for target in self.repository.list_targets(identity.user_id):
            effective_at = str(target.get("effective_at", "") or "")
            try:
                if effective_at and parse_utc(effective_at) <= cutoff:
                    return effective_at
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def preview_payload(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            answers = QuestionnaireAnswers.from_payload(payload)
        except (TypeError, ValueError) as err:
            raise InvalidUserInput(str(err)) from err
        activity_labels = {str(item["value"]): str(item["label"]) for item in ACTIVITY_LEVEL_OPTIONS}
        goal_labels = {str(item["value"]): str(item["label"]) for item in GOAL_OPTIONS}
        return {
            "questionnaire_answers": answers.to_payload(),
            "questionnaire_version": "miniapp-v2",
            "daily_target": derive_daily_target(answers).to_payload(),
            "activity_label": activity_labels[answers.activity_level],
            "goal_label": goal_labels[answers.goal],
        }

    def save_profile(self, identity: ServerlessIdentity, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            answers = QuestionnaireAnswers.from_payload(payload)
        except (TypeError, ValueError) as err:
            raise InvalidUserInput(str(err)) from err
        existing = self.repository.get_profile(identity.user_id)
        timezone_name = str(payload.get("timezone") or (existing.timezone if existing else DEFAULT_TIMEZONE))
        try:
            ZoneInfo(timezone_name)
        except Exception as err:
            raise InvalidUserInput("timezone is invalid") from err
        now = utc_iso(self._now())
        profile = UserProfile(
            telegram_user_id=identity.telegram_user_id,
            username=identity.username or (existing.username if existing else ""),
            display_name=identity.display_name or (existing.display_name if existing else ""),
            daily_target=derive_daily_target(answers),
            questionnaire_answers=answers,
            questionnaire_version="miniapp-v2",
            updated_at=now,
            timezone=timezone_name,
            created_at=existing.created_at if existing else now,
            dietary_preferences=list(payload.get("dietary_preferences", existing.dietary_preferences if existing else [])),
            restrictions=list(payload.get("restrictions", existing.restrictions if existing else [])),
            preferred_cuisines=list(payload.get("preferred_cuisines", existing.preferred_cuisines if existing else [])),
            preferred_staples=list(payload.get("preferred_staples", existing.preferred_staples if existing else [])),
            preferred_tags=list(payload.get("preferred_tags", existing.preferred_tags if existing else [])),
        )
        self.repository.save_profile(identity, profile, effective_at=self._now(), source="miniapp")
        response = self.profile_response(identity)
        response["preview"] = self.preview_payload(payload)
        return response

    def target_history(self, identity: ServerlessIdentity) -> list[dict[str, Any]]:
        return self.repository.list_targets(identity.user_id)

    # ---- Read-only shared workout programme -------------------------------

    def workout_programme(self, version_id: Optional[str] = None) -> dict[str, Any]:
        programme = self.repository.get_workout_programme(version_id=version_id)
        if programme is None:
            raise KeyError(f"Workout programme is unavailable: {PROGRAMME_ID}")
        return programme

    def workout_programme_day(self, day_code: str, version_id: Optional[str] = None) -> dict[str, Any]:
        day = self.repository.get_workout_programme_day(day_code, version_id=version_id)
        if day is None:
            raise KeyError(f"Workout programme day is unavailable: {day_code}")
        return day

    # ---- Durable user-owned workout execution ------------------------------

    def start_workout(self, identity: ServerlessIdentity, day_code: str) -> dict[str, Any]:
        return self.workout_execution.start_session(
            identity,
            day_code,
            actual_local_date=self._local_date(identity),
        )

    def active_workout(self, identity: ServerlessIdentity) -> Optional[dict[str, Any]]:
        return self.workout_execution.get_active_session(identity)

    def workout_session(self, identity: ServerlessIdentity, session_id: str) -> dict[str, Any]:
        return self.workout_execution.get_session(identity, session_id)

    def choose_workout_exercise(self, identity: ServerlessIdentity, session_id: str, execution_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.workout_execution.select_exercise(identity, session_id, execution_id, payload)

    def skip_workout_exercise(self, identity: ServerlessIdentity, session_id: str, execution_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.workout_execution.skip_execution(identity, session_id, execution_id, payload)

    def reset_workout_exercise(self, identity: ServerlessIdentity, session_id: str, execution_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.workout_execution.reset_execution(identity, session_id, execution_id, payload)

    def put_workout_set(self, identity: ServerlessIdentity, session_id: str, execution_id: str, ordinal: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.workout_execution.put_set(identity, session_id, execution_id, ordinal, payload)

    def skip_workout_set(self, identity: ServerlessIdentity, session_id: str, execution_id: str, ordinal: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.workout_execution.skip_set(identity, session_id, execution_id, ordinal, payload)

    def cancel_workout(self, identity: ServerlessIdentity, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.workout_execution.cancel_session(identity, session_id, payload)

    def complete_workout(self, identity: ServerlessIdentity, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.workout_execution.complete_session(identity, session_id, payload)

    def meals_for_date_range(self, identity: ServerlessIdentity, start: date, end: date) -> list[StoredMeal]:
        profile = self.repository.get_profile(identity.user_id)
        timezone_name = profile.timezone if profile else DEFAULT_TIMEZONE
        start_iso, _ = local_day_utc_bounds(start, timezone_name)
        _, end_iso = local_day_utc_bounds(end, timezone_name)
        return self.repository.list_meals_between(identity, start_iso, end_iso, confirmed_only=True)

    def meals_payload(self, identity: ServerlessIdentity, start: Optional[date] = None, end: Optional[date] = None) -> dict[str, Any]:
        active_start = start or self._local_date(identity)
        active_end = end or active_start
        meals = self.meals_for_date_range(identity, active_start, active_end)
        return {
            "date_start": active_start.isoformat(),
            "date_end": active_end.isoformat(),
            "meals": [_meal_payload(meal) for meal in meals],
        }

    def daily_nutrition_payload(self, identity: ServerlessIdentity, target_date: Optional[date] = None) -> dict[str, Any]:
        """Return the read-only dashboard for one user's local calendar day."""

        profile = self.repository.get_profile(identity.user_id)
        timezone_name = profile.timezone if profile else DEFAULT_TIMEZONE
        active_date = target_date or self._local_date(identity)
        start_iso, end_iso = local_day_utc_bounds(active_date, timezone_name)
        daily_summary = self.repository.daily_summary(identity, active_date, timezone_name)
        meals = self.repository.list_meals_between(identity, start_iso, end_iso, confirmed_only=True)

        # Target revisions are effective at an instant. Looking up just before
        # the next local midnight makes a selected historical day use the
        # revision that was active during that day, while keeping the end
        # boundary exclusive for meals.
        target_revision = self.repository.target_revision_at(
            identity.user_id,
            at=parse_utc(end_iso) - timedelta(microseconds=1),
        )
        target = MacroTotal.from_payload(target_revision["target"]) if target_revision else None
        remaining = RemainingMacros.from_target_and_consumed(target, daily_summary.totals) if target else None
        summary_payload = remaining.to_payload() if remaining else {
            "target": None,
            "consumed": daily_summary.totals.to_payload(),
            "remaining_raw": None,
            "remaining": None,
            "over_calories": False,
            "over_protein": False,
            "over_carbs": False,
            "over_fat": False,
        }
        return {
            "date": active_date.isoformat(),
            "today": self._local_date(identity).isoformat(),
            "timezone": timezone_name,
            "target_effective_at": target_revision.get("effective_at") if target_revision else None,
            "target": summary_payload["target"],
            "consumed": summary_payload["consumed"],
            "remaining": summary_payload["remaining"],
            "remaining_raw": summary_payload["remaining_raw"],
            "over_calories": summary_payload["over_calories"],
            "over_protein": summary_payload["over_protein"],
            "over_carbs": summary_payload["over_carbs"],
            "over_fat": summary_payload["over_fat"],
            "meal_count": daily_summary.meal_count,
            "meals": [_meal_payload(meal) for meal in meals],
        }

    def _local_date(self, identity: ServerlessIdentity) -> date:
        profile = self.repository.get_profile(identity.user_id)
        timezone_name = profile.timezone if profile else DEFAULT_TIMEZONE
        return self._now().astimezone(ZoneInfo(timezone_name)).date()

    def current_local_now_utc(self, identity: ServerlessIdentity) -> datetime:
        profile = self.repository.get_profile(identity.user_id)
        timezone_name = profile.timezone if profile else DEFAULT_TIMEZONE
        local_now = self._now().astimezone(ZoneInfo(timezone_name))
        return local_now.astimezone(timezone.utc)

    def should_recommend_after_meal(self, identity: ServerlessIdentity, eaten_at: Optional[str]) -> bool:
        """Suppress a misleading current-day recommendation for historical logs."""

        if not eaten_at:
            return True
        profile = self.repository.get_profile(identity.user_id)
        timezone_name = profile.timezone if profile else DEFAULT_TIMEZONE
        return parse_utc(eaten_at).astimezone(ZoneInfo(timezone_name)).date() == self._local_date(identity)

    # ---- Telegram workflow use cases ------------------------------------------

    def begin_logmeal(self, identity: ServerlessIdentity) -> None:
        self.repository.mark_awaiting_datetime(identity)

    def set_meal_datetime(self, identity: ServerlessIdentity, datetime_iso: str) -> bool:
        return self.repository.set_pending_datetime(identity, datetime_iso)

    def normalize_user_datetime(self, identity: ServerlessIdentity, value: datetime) -> str:
        profile = self.repository.get_profile(identity.user_id)
        timezone_name = profile.timezone if profile else DEFAULT_TIMEZONE
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo(timezone_name))
        return utc_iso(value)

    def consume_meal_datetime(self, identity: ServerlessIdentity) -> Optional[str]:
        return self.repository.consume_pending_datetime(identity)

    def peek_meal_datetime(self, identity: ServerlessIdentity) -> Optional[str]:
        workflow = self.repository.get_workflow(identity.user_id)
        if not workflow or workflow.get("state") != "datetime_selected":
            return None
        value = workflow.get("pending_datetime")
        return str(value) if value else None

    def persona_hint(self, identity: ServerlessIdentity, caption: str) -> str:
        return self.repository.persona_hint(identity, caption)

    def create_pending_meal(self, identity: ServerlessIdentity, **kwargs: Any):
        return self.repository.create_pending_meal(identity, **kwargs)

    def set_action_message_id(self, identity: ServerlessIdentity, token: str, message_id: int) -> None:
        self.repository.set_action_message_id(identity, token, message_id)

    def get_action(self, identity: ServerlessIdentity, token: str):
        return self.repository.get_action(identity, token)

    def find_action_for_update(self, identity: ServerlessIdentity, update_id: int):
        return self.repository.find_action_for_update(identity, update_id)

    def scale_action(self, identity: ServerlessIdentity, token: str, factor: float):
        return self.repository.scale_action(identity, token, factor)

    def set_correction_state(self, identity: ServerlessIdentity, token: str, state: str):
        return self.repository.set_correction_state(identity, token, state)

    def apply_correction(self, identity: ServerlessIdentity, token: str, correction_type: str, correction_value: str):
        return self.repository.apply_correction(identity, token, correction_type, correction_value)

    def finalize_action(self, identity: ServerlessIdentity, token: str, operation: str):
        return self.repository.finalize_action(identity, token, operation)

    def auto_confirm_expired_action(self, identity: ServerlessIdentity, token: str):
        return self.repository.auto_confirm_expired_action(identity, token)

    def expire_pending_actions(self, *, limit: int = 100):
        return self.repository.expire_pending_actions(limit=limit)

    def recommendation(self, identity: ServerlessIdentity):
        target_date = self._local_date(identity)
        prepared = self._planner.prepare(identity.telegram_user_id, target_date=target_date)
        if prepared.skip_reason:
            return self._planner.build_skip_result(prepared), prepared
        return self._planner.build_fallback_result(prepared), prepared

    async def recommendation_async(self, identity: ServerlessIdentity):
        """Use catalogue-only LLM ranking with deterministic fallback."""

        return await self._planner.recommend_next_meal(
            identity.telegram_user_id,
            target_date=self._local_date(identity),
        )


def _meal_to_logged_row(meal: StoredMeal):
    from .serverless_data import _logged_row

    return _logged_row(meal)


def _meal_payload(meal: StoredMeal) -> dict[str, Any]:
    macros = meal.macros.to_payload()
    return {
        "meal_id": meal.meal_id,
        "eaten_at": meal.eaten_at,
        "caption": meal.caption,
        "status": meal.status,
        "adjustment_factor": round(meal.adjustment_factor, 4),
        "macros": macros,
        "original_estimate": meal.original_estimate.to_payload() if hasattr(meal.original_estimate, "to_payload") else _estimate_payload(meal.original_estimate),
    }


def _estimate_payload(estimate: Any) -> dict[str, Any]:
    from .serverless_data import _estimate_payload as serialize_estimate

    return serialize_estimate(estimate)
