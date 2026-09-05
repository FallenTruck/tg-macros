"""Offline recommendation benchmark: properties, time boundaries and hard constraints."""
import asyncio
import json
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from macro_bot.models import (DailyMacroSummary, LoggedMealRow, MacroTotal, UserProfile, RecommendationRequest,
                              RecommendedMeal, RecommendationResult, RemainingMacros)
from macro_bot.recommendations import (RecommendationPlanner, derive_timing, candidate_allowed,
                                       build_ranking_prompt, ServerlessRecommendationClient)
from macro_bot.serverless_service import ReadOnlyFoodCatalogStore
from macro_bot.formatting import format_recommendation_message


def meal(caption="Earlier meal", macros=MacroTotal(1100, 50, 140, 45), at="2026-09-06T12:00:00+08:00"):
    return LoggedMealRow(at, 0, "synthetic", "Synthetic", caption, macros.calories, macros.protein_g,
                         macros.carbs_g, macros.fat_g, 1.0, 1)


class ScenarioTests(unittest.IsolatedAsyncioTestCase):
    def make_planner(self, clock="18:30", consumed=MacroTotal(1100, 50, 140, 45), restrictions=(),
                     entries=None, recent=(), zone="Asia/Singapore", client=None, dietary=()):
        profile = UserProfile(0, "synthetic", "Synthetic", MacroTotal(2100, 150, 220, 70),
                              timezone=zone, restrictions=list(restrictions), dietary_preferences=list(dietary))
        summary = DailyMacroSummary(0, "2026-09-06", consumed, [meal(macros=consumed)])
        repo = SimpleNamespace(get_daily_summary=lambda *_: summary, list_recent_meals=lambda *a, **kw: list(recent))
        catalog = ReadOnlyFoodCatalogStore() if entries is None else SimpleNamespace(list_entries=lambda: entries)
        planner = RecommendationPlanner(repo, SimpleNamespace(get=lambda _: profile), catalog, client,
                                        now_fn=lambda: datetime.fromisoformat(f"2026-09-06T{clock}:00").replace(tzinfo=ZoneInfo(zone)))
        return planner

    async def test_large_protein_gap_at_dinner_allows_full_meal(self):
        result, prepared = await self.make_planner().recommend_next_meal(0)
        self.assertEqual(result.source, "deterministic_fallback")
        self.assertEqual(prepared.candidate_foods[0].meal_type, "full_meal")
        self.assertGreaterEqual(prepared.candidate_foods[0].protein_g, 35)
        self.assertEqual(prepared.timing.likely_next_occasion, "dinner")

    async def test_large_protein_gap_60_minutes_before_bed_prefers_light_protein(self):
        result, prepared = await self.make_planner("22:30").recommend_next_meal(0)
        self.assertEqual(prepared.timing.minutes_until_bedtime, 60)
        self.assertIn(prepared.candidate_foods[0].meal_type, {"light_meal", "protein_top_up", "snack"})
        self.assertLessEqual(prepared.candidate_foods[0].fat_g, 12)
        self.assertIn("substantial gap", result.summary)
        self.assertIn("60 minutes", format_recommendation_message(result))
        full = [c for c in prepared.candidate_foods if c.meal_type == "full_meal"]
        self.assertTrue(full, "full meals remain possible tradeoffs")
        self.assertGreater(prepared.candidate_foods[0].fit_score, max(c.fit_score for c in full))

    async def test_low_fat_remaining(self):
        p = self.make_planner(consumed=MacroTotal(1100, 50, 140, 67)).prepare(0)
        self.assertLessEqual(p.candidate_foods[0].fat_g, 12)
        self.assertIn("fat is constrained", p.strategy_signal)

    async def test_carb_heavy_day_penalizes_another_large_base(self):
        planner = self.make_planner(consumed=MacroTotal(1200, 60, 190, 35))
        p = planner.prepare(0)
        entry = next(c for c in planner._food_catalog_store.list_entries() if c.food_id == "dal_rice_bowl")
        no_history, _ = planner._score_entry(entry, p.profile, p.remaining, "")
        with_history, reason = planner._score_entry(entry, p.profile, p.remaining, "", p.daily_summary.meals)
        self.assertEqual(no_history - with_history, 14)
        self.assertIn("carb-heavy", reason)
        self.assertLessEqual(p.candidate_foods[0].carbs_g, 55)

    async def test_very_low_calories_only_small_protein_gap_options(self):
        result, p = await self.make_planner(consumed=MacroTotal(2050, 100, 220, 70)).recommend_next_meal(0)
        self.assertTrue(result.suggestions, "meaningful protein gaps are not blindly suppressed")
        self.assertTrue(all(c.calories <= 250 and c.protein_g >= 20 for c in p.candidate_foods))
        result, _ = await self.make_planner(consumed=MacroTotal(2050, 145, 220, 70)).recommend_next_meal(0)
        self.assertEqual(result.source, "skipped")

    async def test_vegetarian_and_other_restrictions_filter_before_model(self):
        client = SimpleNamespace(recommend=AsyncMock(side_effect=RuntimeError("unavailable")))
        _, p = await self.make_planner(restrictions=["vegetarian"], client=client).recommend_next_meal(0)
        self.assertTrue(p.candidate_foods)
        self.assertTrue(all("vegetarian" in c.tags for c in client.recommend.call_args.args[0].candidate_foods))
        for restrictions in (["vegan"], ["dairy-free"], ["unknown_allergy"]):
            q = self.make_planner(restrictions=restrictions).prepare(0)
            if restrictions == ["unknown_allergy"]:
                self.assertFalse(q.candidate_foods)
            elif restrictions == ["vegan"]:
                self.assertTrue(all("vegan" in c.tags for c in q.candidate_foods))
            else:
                self.assertTrue(q.candidate_foods)
                self.assertTrue(all("dairy_free" in c.tags for c in q.candidate_foods))
        q = self.make_planner(dietary=["vegetarian"]).prepare(0)
        self.assertTrue(all("vegetarian" in c.tags for c in q.candidate_foods))
        entry = ReadOnlyFoodCatalogStore().list_entries()[0]
        self.assertFalse(candidate_allowed(replace(entry, tags=["vegetarian"], contains=["poultry"]), p.profile, 0))

    async def test_repeated_recent_foods_rank_lower(self):
        planner = self.make_planner()
        baseline = planner.prepare(0)
        best = baseline.candidate_foods[0]
        repeated = self.make_planner(recent=[meal(best.name)]).prepare(0)
        candidate = next((c for c in repeated.candidate_foods if c.food_id == best.food_id), None)
        self.assertTrue(candidate is None or candidate.fit_score < best.fit_score)
        self.assertNotEqual(repeated.candidate_foods[0].food_id, best.food_id)

    async def test_historical_log_skips_model(self):
        client = SimpleNamespace(recommend=AsyncMock())
        result, _ = await self.make_planner(client=client).recommend_next_meal(0, date(2026, 9, 5))
        self.assertEqual(result.source, "skipped")
        client.recommend.assert_not_called()

    async def test_targets_nearly_met_and_no_valid_candidates(self):
        for planner in (self.make_planner(consumed=MacroTotal(2050, 145, 215, 68)), self.make_planner(entries=[]),
                        self.make_planner("23:20", consumed=MacroTotal(1850, 140, 200, 60))):
            result, _ = await planner.recommend_next_meal(0)
            self.assertEqual(result.source, "skipped")
            self.assertEqual(format_recommendation_message(result), "")

    async def test_bedtime_small_topup_only_as_default_with_gap(self):
        for clock in ("22:46", "23:15", "23:45", "00:15"):
            result, p = await self.make_planner(clock).recommend_next_meal(0)
            self.assertTrue(result.suggestions)
            self.assertLessEqual(p.candidate_foods[0].calories, 250)
            self.assertEqual(p.candidate_foods[0].meal_type, "protein_top_up")

    async def test_timing_boundaries_timezones_and_recent_meals(self):
        for clock, band in [("20:29", "full_meal"), ("20:30", "moderate"), ("22:00", "light"),
                            ("22:45", "top_up"), ("23:15", "bedtime")]:
            p = self.make_planner(clock, zone="America/New_York").prepare(0)
            self.assertEqual(p.timing.band, band)
            self.assertTrue(p.timing.target_bedtime.endswith("23:30-04:00"))
        now = datetime(2026, 9, 6, 19, tzinfo=ZoneInfo("Asia/Singapore"))
        t = derive_timing(now, [meal(at="2026-09-06T10:00:00Z")])
        self.assertEqual(t.likely_next_occasion, "late_evening_snack_top_up")
        self.assertTrue(t.most_recent_meal_time.endswith("18:00+08:00"))
        self.assertGreater(derive_timing(now.replace(hour=9), []).remaining_eating_occasions, 1)
        self.assertLess(derive_timing(now.replace(hour=0), []).minutes_until_bedtime, 0)

    async def test_explicit_eligibility_and_availability_still_apply(self):
        entry = ReadOnlyFoodCatalogStore().list_entries()[0]
        for changed in (replace(entry, available=False), replace(entry, eligible_telegram_user_ids=[123])):
            self.assertFalse(self.make_planner(entries=[changed]).prepare(0).candidate_foods)
        self.assertTrue(all(not e.eligible_telegram_user_ids for e in ReadOnlyFoodCatalogStore().list_entries()))

    async def test_documented_timing_weights_and_catalogue_metadata_roundtrip(self):
        from macro_bot.models import FoodCatalogEntry, CandidateFood
        entries = ReadOnlyFoodCatalogStore().list_entries()
        self.assertEqual({entry.meal_type for entry in entries}, {'full_meal', 'light_meal', 'snack', 'protein_top_up'})
        for entry in entries:
            self.assertEqual(FoodCatalogEntry.from_payload(entry.to_payload()), entry)
            candidate = CandidateFood.from_catalog(entry)
            self.assertEqual(CandidateFood.from_payload(candidate.to_payload()).meal_type, entry.meal_type)
        heavy = replace(entries[0], macros=MacroTotal(620, 40, 60, 26), tags=['heavy', 'high_fat'])
        light = next(entry for entry in entries if entry.food_id == 'whey_water')
        for clock, penalty, bonus in [('18:30', 0, 0), ('21:00', -37, 8), ('22:30', -130, 30),
                                      ('23:00', -220, 40), ('23:20', -275, 50)]:
            planner = self.make_planner(clock)
            timing = planner.prepare(0).timing
            self.assertEqual(planner._timing_score(heavy, timing)[0], penalty)
            self.assertEqual(planner._timing_score(light, timing)[0], bonus)

    async def test_invalid_model_ids_fall_back_and_macros_are_rebound(self):
        for invalid in (True, False):
            async def rank(request):
                candidate = request.candidate_foods[-1]
                suggestion = RecommendedMeal.from_candidate(candidate, "A concise reason", "A tradeoff")
                suggestion = replace(suggestion, calories=9999, candidate_id="invented" if invalid else candidate.food_id)
                return RecommendationResult("Made-up totals", MacroTotal(9999, 0, 0, 0), MacroTotal(0, 0, 0, 0), [suggestion], "model_ranked")
            result, p = await self.make_planner("22:30", client=SimpleNamespace(recommend=rank)).recommend_next_meal(0)
            self.assertEqual(result.source, "deterministic_fallback" if invalid else "model_ranked")
            self.assertEqual(result.today_totals, p.daily_summary.totals)
            self.assertEqual(result.suggestions[0].candidate_id, p.candidate_foods[0].food_id)
            self.assertTrue(all(c.calories < 9999 for c in result.suggestions))
            self.assertNotIn("Made-up", result.summary)

    async def test_prompt_contains_bounded_timing_and_authoritative_candidate_metadata(self):
        client = SimpleNamespace(recommend=AsyncMock(side_effect=TimeoutError()))
        await self.make_planner("22:30", client=client).recommend_next_meal(0)
        request = client.recommend.call_args.args[0]
        prompt = build_ranking_prompt(request)
        for text in ('minutes_until_bedtime', 'target_bedtime', 'meal_type', 'fit_score', 'local_time', 'remaining_eating_occasions'):
            self.assertIn(text, prompt)
        self.assertLess(len(prompt), 10000)
        self.assertNotIn('telegram_user_id', prompt)
        self.assertNotIn('username', prompt)

    async def test_user_explanations_do_not_expose_internal_scores(self):
        async def rank(request):
            return RecommendationResult('summary', request.today_totals, request.remaining.remaining,
                [RecommendedMeal.from_candidate(request.candidate_foods[0], 'best fit score', 'ranked by the model')], 'model_ranked')
        result, _ = await self.make_planner(client=SimpleNamespace(recommend=rank)).recommend_next_meal(0)
        message = format_recommendation_message(result)
        self.assertNotIn('score', message)
        self.assertNotIn('model', message)

    async def test_serverless_model_schema_and_invalid_ids(self):
        p = self.make_planner().prepare(0)
        request = RecommendationRequest(0, p.profile, p.daily_summary.totals, p.remaining, [], p.candidate_foods)
        responses = Mock()
        responses.create.return_value = SimpleNamespace(output_text=json.dumps({"summary": "Fit", "suggestions": [
            {"candidate_id": "invented", "reason": "okay", "tradeoff": "none"}]}))
        with patch('openai.OpenAI', return_value=Mock(responses=responses)), patch('macro_bot.serverless_auth.openai_key_from_environment', return_value='test-key'):
            with self.assertRaises(ValueError):
                await ServerlessRecommendationClient().recommend(request)
        self.assertTrue(responses.create.call_args.kwargs['text']['format']['strict'])


if __name__ == '__main__':
    unittest.main()
