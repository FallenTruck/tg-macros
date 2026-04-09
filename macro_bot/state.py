from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from .models import MealEstimate, PendingMealAction


class MealWorkflowStore:
    def __init__(
        self,
        bot_data: dict,
        action_ttl_seconds: int,
        datetime_ttl_seconds: int,
    ):
        self._bot_data = bot_data
        self._action_ttl = timedelta(seconds=action_ttl_seconds)
        self._datetime_ttl = timedelta(seconds=datetime_ttl_seconds)

        self._awaiting_dt: Set[int] = bot_data.setdefault("awaiting_meal_dt", set())
        self._pending_dt: Dict[int, dict] = bot_data.setdefault("pending_meal_dt", {})
        self._meal_actions: Dict[str, PendingMealAction] = bot_data.setdefault("meal_actions", {})
        self._user_map: Dict[int, str] = bot_data.setdefault("user_map", {})
        self._user_meal_history: Dict[int, List[dict]] = bot_data.setdefault("user_meal_history", {})

    def cleanup(self) -> None:
        now = datetime.utcnow()

        expired_users = [
            user_id
            for user_id, payload in self._pending_dt.items()
            if now - datetime.fromisoformat(payload["created_at"]) > self._datetime_ttl
        ]
        for user_id in expired_users:
            self._pending_dt.pop(user_id, None)
            self._awaiting_dt.discard(user_id)

        expired_tokens = [
            token
            for token, action in self._meal_actions.items()
            if now - action.created_at > self._action_ttl
        ]
        for token in expired_tokens:
            self._meal_actions.pop(token, None)

    def set_user_person(self, user_id: int, person: str) -> None:
        self._user_map[user_id] = person

    def get_user_person(self, user_id: int) -> Optional[str]:
        return self._user_map.get(user_id)

    def mark_awaiting_datetime(self, user_id: int) -> None:
        self._awaiting_dt.add(user_id)

    def is_awaiting_datetime(self, user_id: int) -> bool:
        return user_id in self._awaiting_dt

    def set_pending_datetime(self, user_id: int, dt_iso: str) -> None:
        self._pending_dt[user_id] = {
            "datetime": dt_iso,
            "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        }
        self._awaiting_dt.discard(user_id)

    def pop_pending_datetime(self, user_id: int) -> Optional[str]:
        payload = self._pending_dt.pop(user_id, None)
        if not payload:
            return None
        return payload["datetime"]

    def add_action(self, action: PendingMealAction) -> None:
        self._meal_actions[action.token] = action

    def get_action(self, token: str) -> Optional[PendingMealAction]:
        return self._meal_actions.get(token)

    def get_persona_hint(self, user_id: int, caption: str) -> str:
        history = self._user_meal_history.get(user_id, [])
        caption_norm = self._normalize_caption(caption)
        if not caption_norm:
            return ""

        for entry in reversed(history):
            if entry["caption_norm"] == caption_norm:
                return (
                    "Similar prior meal detected. Prior estimate context: "
                    f"{entry['summary']}. Use this as a soft prior only if image appears similar."
                )
        return ""

    def record_confirmed_meal(self, user_id: int, caption: str, estimate: MealEstimate) -> None:
        caption_norm = self._normalize_caption(caption)
        if not caption_norm:
            return

        low = int(round(estimate.total_low.calories)) if estimate.total_low else None
        high = int(round(estimate.total_high.calories)) if estimate.total_high else None
        range_text = f", range={low}-{high} kcal" if low is not None and high is not None else ""
        summary = (
            f"meal={estimate.meal_name}, calories={int(round(estimate.calories))} kcal"
            f"{range_text}, assumptions={estimate.assumptions_summary(max_chars=80)}"
        )

        entries = self._user_meal_history.setdefault(user_id, [])
        entries.append(
            {
                "caption_norm": caption_norm,
                "summary": summary,
                "created_at": datetime.utcnow().isoformat(timespec="seconds"),
            }
        )
        self._user_meal_history[user_id] = entries[-20:]

    @staticmethod
    def _normalize_caption(text: str) -> str:
        return " ".join((text or "").strip().lower().split())
