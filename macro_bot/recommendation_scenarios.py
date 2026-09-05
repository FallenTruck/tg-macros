"""Fixed, IAM-only retrospective smoke fixtures for the marked dev account.

No estimator, external message, caller-selected identity or clock is accepted.
The runner resets the synthetic partition before each case and in cleanup.
"""
from dataclasses import asdict, replace
from datetime import timedelta
from zoneinfo import ZoneInfo

from .formatting import format_nutrition_state_message, format_recommendation_message
from .models import MealEstimate
from .nutrition_lab import require_identity
from .serverless_data import DynamoNutritionRepository
from .serverless_service import NutritionService

SCENARIOS = ('current_meal', 'same_day_backfill', 'incomplete_backfill', 'previous_day', 'custom_bedtime')


async def run_scenario(service, scenario):
    if scenario not in SCENARIOS:
        raise ValueError('Unknown retrospective scenario')
    identity = require_identity(service.repository)
    profile = service.repository.get_profile(identity.user_id)
    if profile is None:
        raise ValueError('Reset the synthetic profile first')
    # Two days ahead avoids both expiry sweeps and the reset's current-day meal. Calendar accounting
    # remains real; the recommendation clock is explicitly injected for this smoke.
    day = service._now().astimezone(ZoneInfo(profile.timezone)) + timedelta(days=2)
    now = day.replace(hour=18 if scenario == 'current_meal' else 22,
                      minute=30 if scenario == 'current_meal' else 0, second=0, microsecond=0)
    clock = [now]
    original = service.repository
    repo = DynamoNutritionRepository(original.table, table_name=original.table_name,
                                      client=original.client, now_fn=lambda: clock[0])
    flow = NutritionService(repo, catalog_store=service.catalog_store, now_fn=lambda: clock[0])
    flow._planner._recommendation_client = service._planner._recommendation_client
    fixture_profile = replace(profile, recommendation_bedtime='22:45' if scenario == 'custom_bedtime' else '23:30')
    repo.save_profile(identity, fixture_profile, append_target=False)
    assert not flow.daily_nutrition_payload(identity, now.date())['meals'], 'Reset the synthetic baseline first'

    def log(caption, hour, minute, macros, *, previous_day=False, confirmed=None):
        clock[0] = confirmed or now
        eaten = now.replace(hour=hour, minute=minute) - timedelta(days=int(previous_day))
        # Exercise the same selected-datetime workflow used by /logmeal.
        flow.begin_logmeal(identity)
        assert flow.set_meal_datetime(identity, flow.normalize_user_datetime(identity, eaten))
        from .serverless_data import parse_utc
        action = flow.create_pending_meal(identity, chat_id=0, request_message_id=0, caption=caption,
            estimate=MealEstimate(caption, *macros, 1.0, 'Synthetic fixed macros'),
            eaten_at=parse_utc(flow.peek_meal_datetime(identity)))
        flow.consume_meal_datetime(identity)
        return flow.finalize_action(identity, action.token, 'confirm').meal

    if scenario == 'same_day_backfill':
        log('Synthetic lunch', 13, 0, (600, 35, 70, 20), confirmed=now.replace(hour=13))
        log('Synthetic dinner', 19, 15, (640, 43, 70, 20), confirmed=now.replace(hour=19, minute=15))
    if scenario in ('same_day_backfill', 'incomplete_backfill', 'previous_day'):
        meal = log('Synthetic breakfast', 8, 30, (420, 24, 46, 15), previous_day=scenario == 'previous_day')
    else:
        meal = log('Synthetic current meal', now.hour, now.minute, (640, 43, 70, 20))
    clock[0] = now
    state = flow.confirmed_nutrition_payload(identity, meal.meal_id)
    allowed = flow.should_recommend_after_meal(identity, meal.eaten_at)
    if allowed:
        result, prepared = await flow.recommendation_async(identity)
    else:
        from datetime import date
        planner = flow._planner_for_identity(identity)
        prepared = planner.prepare(0, target_date=date.fromisoformat(state['date']))
        result = planner.build_skip_result(prepared)
    return {'scenario': scenario, 'source': result.source, 'strategy_version': result.strategy_version,
            'timing': asdict(prepared.timing), 'today_meals': prepared.today_meals,
            'candidates': [item.to_payload() for item in prepared.candidate_foods],
            'suggestions': [item.candidate_id for item in result.suggestions],
            'recommendation_allowed': allowed, 'meal_status': meal.status,
            'eaten_at': meal.eaten_at, 'confirmed_at': meal.confirmed_at,
            'entry_delay_minutes': meal.entry_delay_minutes,
            'logged_day': state['date'], 'today': state['today'], 'logged_day_consumed': state['consumed'],
            'current_day_consumed': flow.daily_nutrition_payload(identity, now.date())['consumed'],
            'state_preview': format_nutrition_state_message(state),
            'preview': format_recommendation_message(result)}
