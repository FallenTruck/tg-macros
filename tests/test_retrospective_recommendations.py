"""Actual meal chronology, fixed-clock retrospective cases and bedtime boundaries."""
import copy
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from macro_bot.models import LoggedMealRow, RecommendedMeal, RecommendationResult
from macro_bot.recommendations import derive_timing
from macro_bot.recommendation_scenarios import run_scenario
from tests import test_nutrition_lab as lab_fixtures
from tests.test_serverless_data import _estimate


class RetrospectiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = lab_fixtures.LabTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        async def rank(request):
            return RecommendationResult('You still need everything', request.today_totals, request.remaining.remaining,
                [RecommendedMeal.from_candidate(c, 'You still need everything', '') for c in request.candidate_foods[:3]], 'model_ranked')
        self.client = SimpleNamespace(recommend=AsyncMock(side_effect=rank))
        self.fixture.service._planner._recommendation_client = self.client

    async def test_current_meal_uses_current_clock_without_retrospective_title(self):
        result = await run_scenario(self.fixture.service, 'current_meal')
        self.assertEqual(result['eaten_at'], result['confirmed_at'])
        self.assertEqual(result['entry_delay_minutes'], 0)
        self.assertTrue(result['state_preview'].startswith('✅ Meal logged\n'))
        self.assertEqual(result['timing']['minutes_until_bedtime'], 300)
        self.assertFalse(result['timing']['possible_incomplete_day'])

    async def test_breakfast_entered_after_dinner_keeps_actual_order_and_now(self):
        result = await run_scenario(self.fixture.service, 'same_day_backfill')
        self.assertEqual([m['local_time'][11:16] for m in result['today_meals']], ['08:30', '13:00', '19:15'])
        self.assertEqual(result['timing']['most_recent_meal_time'][11:16], '19:15')
        self.assertEqual(result['timing']['local_datetime'][11:16], '22:00')
        self.assertEqual(result['timing']['minutes_until_bedtime'], 90)
        self.assertFalse(result['timing']['possible_incomplete_day'])
        self.assertEqual(result['logged_day_consumed']['calories'], 1660)
        self.assertEqual(result['entry_delay_minutes'], 810)
        self.assertIn('Meal logged for 8:30 AM', result['state_preview'])
        request = self.client.recommend.call_args.args[0]
        self.assertEqual([m['caption'] for m in request.today_meals], ['Synthetic breakfast', 'Synthetic lunch', 'Synthetic dinner'])

    async def test_sparse_backfill_qualifies_prose_and_moderates_large_gaps(self):
        result = await run_scenario(self.fixture.service, 'incomplete_backfill')
        self.assertTrue(result['timing']['possible_incomplete_day'])
        self.assertIn("Based on what you've logged today", result['preview'])
        self.assertIn("If today's log is complete", result['preview'])
        self.assertNotIn('You still need', result['preview'])
        best = next(c for c in result['candidates'] if c['food_id'] == result['suggestions'][0])
        self.assertNotEqual(best['meal_type'], 'full_meal')
        self.assertLessEqual(best['calories'], 450)
        self.assertGreaterEqual(best['protein_g'], 20)
        self.assertTrue(self.client.recommend.called)

    async def test_previous_day_updates_only_historical_totals_and_never_ranks(self):
        # The real reset seeds today's baseline meal. It must not pollute the fixed fixture days.
        f = self.fixture
        action = f.repo.create_pending_meal(f.identity, chat_id=0, request_message_id=0,
            caption='E2E baseline meal', estimate=_estimate(780), eaten_at=f.service._now())
        f.repo.finalize_action(f.identity, action.token, 'confirm')
        result = await run_scenario(self.fixture.service, 'previous_day')
        self.assertEqual(result['meal_status'], 'confirmed')
        self.assertNotEqual(result['logged_day'], result['today'])
        self.assertEqual(result['logged_day_consumed']['calories'], 420)
        self.assertEqual(result['current_day_consumed']['calories'], 0)
        self.assertFalse(result['recommendation_allowed'])
        self.assertEqual(result['source'], 'skipped')
        self.assertEqual(result['preview'], '')
        self.client.recommend.assert_not_called()

    async def test_custom_bedtime_shifts_deployed_scenario(self):
        result = await run_scenario(self.fixture.service, 'custom_bedtime')
        self.assertEqual(result['timing']['minutes_until_bedtime'], 45)
        self.assertEqual(result['timing']['band'], 'top_up')
        self.assertIn('22:45', result['preview'])
        self.assertNotIn('23:30', result['preview'])

    async def test_scenario_rejects_unknown_case_and_unmarked_identity_before_writes(self):
        before = copy.deepcopy(self.fixture.table.items)
        with self.assertRaises(ValueError):
            await run_scenario(self.fixture.service, 'arbitrary')
        self.assertEqual(before, self.fixture.table.items)
        self.fixture.table.items[('E2E_ACCOUNT#javaan-e2e', 'META')]['account_type'] = 'real'
        before = copy.deepcopy(self.fixture.table.items)
        with self.assertRaises(PermissionError):
            await run_scenario(self.fixture.service, 'current_meal')
        self.assertEqual(before, self.fixture.table.items)

    async def test_confirmation_is_once_only_and_legacy_timestamp_stays_unknown(self):
        f = self.fixture
        action = f.repo.create_pending_meal(f.identity, chat_id=0, request_message_id=0, caption='Synthetic', estimate=_estimate(), eaten_at=f.now-timedelta(hours=4))
        first = f.repo.finalize_action(f.identity, action.token, 'confirm').meal
        f.now += timedelta(minutes=5)
        duplicate = f.repo.finalize_action(f.identity, action.token, 'confirm')
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(first.confirmed_at, duplicate.meal.confirmed_at)
        item = f.table.items[(f.identity.pk, action.canonical_sk)]
        del item['confirmed_at']
        legacy = f.repo.get_meal(f.identity, action.meal_id)
        self.assertIsNone(legacy.confirmed_at)
        self.assertIsNone(legacy.entry_delay_minutes)
        self.assertEqual(first.eaten_at, legacy.eaten_at)
        expired = f.repo.create_pending_meal(f.identity, chat_id=0, request_message_id=1, caption='Synthetic', estimate=_estimate(), eaten_at=f.now-timedelta(hours=3), action_ttl_seconds=10)
        f.now += timedelta(seconds=11)
        meal = f.repo.auto_confirm_expired_action(f.identity, expired.token).meal
        self.assertEqual(meal.confirmed_at, f.now.isoformat().replace('+00:00', 'Z'))
        self.assertEqual(meal.entry_delay_minutes, 180)


class BedtimeTests(unittest.TestCase):
    def test_default_custom_midnight_and_timezone(self):
        zone = ZoneInfo('America/New_York')
        now = datetime(2026, 9, 6, 22, tzinfo=zone)
        self.assertEqual(derive_timing(now, []).minutes_until_bedtime, 90)
        self.assertEqual(derive_timing(now, [], '22:45').minutes_until_bedtime, 45)
        self.assertEqual(derive_timing(now, [], '00:30').minutes_until_bedtime, 150)
        self.assertEqual(derive_timing(now.replace(hour=0, minute=10), [], '00:30').minutes_until_bedtime, 20)
        self.assertEqual(derive_timing(now.replace(hour=1), [], '00:30').minutes_until_bedtime, -30)
        self.assertEqual(derive_timing(now.replace(hour=0), []).minutes_until_bedtime, -30)
        same_instant = now.astimezone(timezone.utc).astimezone(zone)
        self.assertEqual(derive_timing(same_instant, [], '22:45').target_bedtime, now.replace(hour=22, minute=45).isoformat(timespec='minutes'))

    def test_incomplete_thresholds_and_no_claim_from_unknown_confirmations(self):
        zone = ZoneInfo('Asia/Singapore')
        now = datetime(2026, 9, 6, 22, tzinfo=zone)
        early = LoggedMealRow('2026-09-06T08:30:00+08:00',0,'test','Synthetic','Breakfast',420,24,46,15,1,0,now.isoformat())
        self.assertTrue(derive_timing(now, [early]).possible_incomplete_day)
        for row in (replace(early, confirmed_at=None), replace(early, confirmed_at=now.replace(hour=9).isoformat()),
                    replace(early, confirmed_at=now.replace(hour=18).isoformat()), replace(early, datetime_iso=now.replace(hour=21).isoformat())):
            self.assertFalse(derive_timing(now, [row]).possible_incomplete_day)
        self.assertFalse(derive_timing(now.replace(hour=19), [replace(early, confirmed_at=now.replace(hour=19).isoformat())]).possible_incomplete_day)

    def test_incomplete_signal_changes_scoring_even_before_late_bedtime_bands(self):
        from tests.test_recommendation_scenarios import ScenarioTests
        helper = ScenarioTests()
        planner = helper.make_planner('20:00')
        prepared = planner.prepare(0)
        timing = prepared.timing
        self.assertEqual(timing.band, 'full_meal')
        uncertain = replace(timing, possible_incomplete_day=True)
        entry = next(e for e in planner._food_catalog_store.list_entries() if e.food_id == 'chicken_rice_bowl')
        normal, _ = planner._score_entry(entry, prepared.profile, prepared.remaining, '', timing=timing)
        moderated, _ = planner._score_entry(entry, prepared.profile, prepared.remaining, '', timing=uncertain)
        self.assertLess(moderated, normal - 20)

    def test_dst_repeated_hour_keeps_actual_latest_meal_and_elapsed_bedtime(self):
        zone = ZoneInfo('America/New_York')
        now = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=1)
        first = LoggedMealRow('2026-11-01T01:50:00-04:00',0,'test','Synthetic','Earlier',100,10,10,1,1,0)
        second = replace(first, datetime_iso='2026-11-01T01:30:00-05:00', caption='Later')
        timing = derive_timing(now, [second, first], '03:00')
        self.assertEqual(timing.most_recent_meal_time, '2026-11-01T01:30-05:00')
        self.assertEqual(timing.minutes_until_bedtime, 75)
