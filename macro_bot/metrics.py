import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import PendingMealAction, RecommendationResult
from .recommendations import PreparedRecommendation

METRICS_DIR = Path(__file__).resolve().parent.parent / "metrics"
ESTIMATES_LOG_PATH = METRICS_DIR / "estimates.jsonl"
RECOMMENDATIONS_LOG_PATH = METRICS_DIR / "recommendations.jsonl"


def append_outcome_event(
    action: PendingMealAction,
    final_action: str,
    test_label: str,
    person: Optional[str] = None,
) -> None:
    event_id = action.metrics_event_id
    if not event_id:
        return

    record = {
        "event_type": "estimate_outcome",
        "event_id": event_id,
        "timestamp_utc": datetime.utcnow().isoformat(timespec="seconds"),
        "final_action": final_action,
        "test_label": test_label,
        "telegram_user_id": action.telegram_user_id,
        "person": person or "unknown",
        "caption": action.caption,
        "adjustment_factor": round(action.adjustment_factor, 4),
        "estimate": {
            "meal_name": action.estimate.meal_name,
            "calories": int(round(action.estimate.calories)),
            "protein_g": round(action.estimate.protein_g, 1),
            "carbs_g": round(action.estimate.carbs_g, 1),
            "fat_g": round(action.estimate.fat_g, 1),
            "confidence": round(float(action.estimate.confidence), 3),
            "range_low_kcal": int(round(action.estimate.total_low.calories))
            if action.estimate.total_low
            else None,
            "range_high_kcal": int(round(action.estimate.total_high.calories))
            if action.estimate.total_high
            else None,
        },
    }
    _append_jsonl_record(ESTIMATES_LOG_PATH, record)


def append_recommendation_event(
    telegram_user_id: int,
    trigger_source: str,
    prepared: PreparedRecommendation,
    result: RecommendationResult,
) -> None:
    record = {
        "event_type": "recommendation",
        "timestamp_utc": datetime.utcnow().isoformat(timespec="seconds"),
        "telegram_user_id": telegram_user_id,
        "trigger_source": trigger_source,
        "profile": prepared.profile.to_payload(),
        "today_totals": prepared.daily_summary.totals.to_payload(),
        "remaining": prepared.remaining.to_payload(),
        "candidate_foods": [item.to_payload() for item in prepared.candidate_foods],
        "result": result.to_payload(),
    }
    _append_jsonl_record(RECOMMENDATIONS_LOG_PATH, record)


def append_recommendation_skip_event(
    telegram_user_id: int,
    trigger_source: str,
    prepared: PreparedRecommendation,
) -> None:
    record = {
        "event_type": "recommendation_skipped",
        "timestamp_utc": datetime.utcnow().isoformat(timespec="seconds"),
        "telegram_user_id": telegram_user_id,
        "trigger_source": trigger_source,
        "skip_reason": prepared.skip_reason,
        "today_totals": prepared.daily_summary.totals.to_payload(),
        "remaining": prepared.remaining.to_payload(),
    }
    _append_jsonl_record(RECOMMENDATIONS_LOG_PATH, record)


def _append_jsonl_record(path: Path, record: dict) -> None:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        with path.open("rb+") as metrics_file:
            metrics_file.seek(-1, 2)
            if metrics_file.read(1) != b"\n":
                metrics_file.write(b"\n")
    with path.open("a", encoding="utf-8") as metrics_file:
        metrics_file.write(json.dumps(record, ensure_ascii=True))
        metrics_file.write("\n")
