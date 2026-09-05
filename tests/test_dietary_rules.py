"""Hard food-rule enforcement before ranking, independent of model/macros/clock."""
import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from macro_bot.models import (DailyMacroSummary, FoodCatalogEntry, MacroTotal, RecommendedMeal,
                              RecommendationResult, UserProfile)
from macro_bot.recommendations import RecommendationPlanner, candidate_allowed, build_ranking_prompt
from macro_bot.serverless_service import NutritionService, ReadOnlyFoodCatalogStore, InvalidUserInput
from macro_bot.serverless_data import DynamoNutritionRepository
from macro_bot.storage import UserProfileStore
from tests.test_serverless_data import _FakeTable
from scripts.e2e_support import BASELINE_PROFILE_PAYLOAD


CATALOG = {entry.food_id: entry for entry in ReadOnlyFoodCatalogStore().list_entries()}


def profile(**rules):
    return UserProfile(0, 'synthetic', 'Synthetic', MacroTotal(2100,150,220,70), **rules)


def planner(saved_profile, entries, clock='18:30', recent=()):
    async def rank(request):
        return RecommendationResult('Valid selection', request.today_totals, request.remaining.remaining,
            [RecommendedMeal.from_candidate(candidate, 'Fits the occasion', '') for candidate in request.candidate_foods[:3]], 'model_ranked')
    client = SimpleNamespace(recommend=AsyncMock(side_effect=rank))
    repo = SimpleNamespace(get_daily_summary=lambda *_: DailyMacroSummary(0,'2026-09-06',MacroTotal(1100,50,140,45),[]),
                           list_recent_meals=lambda *a, **kw: [SimpleNamespace(caption=value) for value in recent])
    result = RecommendationPlanner(repo, SimpleNamespace(get=lambda _: saved_profile),
        SimpleNamespace(list_entries=lambda: entries), client,
        now_fn=lambda: datetime.fromisoformat('2026-09-06T'+clock+':00').replace(tzinfo=ZoneInfo('Asia/Singapore')))
    return result, client


class DietaryScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_vegetarian_with_eggs_allowed(self):
        p = profile(diet_type='vegetarian', eggs_allowed=True)
        entries = [CATALOG[key] for key in ('egg_toast_plate','chicken_rice_bowl','tofu_clear_soup')]
        flow, client = planner(p, entries)
        result, _ = await flow.recommend_next_meal(0)
        supplied = client.recommend.call_args.args[0].candidate_foods
        self.assertIn('egg_toast_plate', [c.food_id for c in supplied])
        self.assertNotIn('chicken_rice_bowl', [c.food_id for c in supplied])
        self.assertEqual(result.source, 'model_ranked')
        # An egg allowance never permits poultry/meat, even if a tag is contradictory.
        mislabeled = replace(CATALOG['chicken_rice_bowl'], tags=['ovo_vegetarian'])
        self.assertFalse(candidate_allowed(mislabeled, p, 0))

    async def test_vegetarian_with_eggs_not_allowed_or_unspecified(self):
        for rule in (False, None):
            p = profile(diet_type='vegetarian', eggs_allowed=rule)
            entries = [CATALOG[key] for key in ('egg_toast_plate','chicken_rice_bowl','tofu_clear_soup')]
            flow, client = planner(p, entries)
            await flow.recommend_next_meal(0)
            self.assertEqual([c.food_id for c in client.recommend.call_args.args[0].candidate_foods], ['tofu_clear_soup'])
            self.assertFalse(candidate_allowed(replace(CATALOG['egg_toast_plate'], tags=['vegetarian','egg_free']), p, 0))

    async def test_dairy_restriction_and_allergy_before_model(self):
        entries = [CATALOG[key] for key in ('double_protein_shake','plain_greek_yogurt','paneer_wrap','soy_protein_water')]
        for rules in ({'dairy_allowed':False}, {'allergens':['milk']}, {'restrictions':['dairy-free']},
                      {'dairy_allowed':True, 'restrictions':['milk_allergy']},
                      {'dairy_allowed':True, 'dietary_preferences':['dairy_free']}):
            flow, client = planner(profile(**rules), entries)
            await flow.recommend_next_meal(0)
            self.assertEqual([c.food_id for c in client.recommend.call_args.args[0].candidate_foods], ['soy_protein_water'])
        unknown = replace(CATALOG['whey_water'], contains=[], ingredients=[], tags=[])
        self.assertFalse(candidate_allowed(unknown, profile(dairy_allowed=False), 0))
        contradiction = replace(CATALOG['whey_water'], tags=['vegan','dairy_free'])
        self.assertFalse(candidate_allowed(contradiction, profile(dairy_allowed=False), 0))

    async def test_prohibited_ingredient_uses_metadata_and_unknown_fails_closed(self):
        safe = replace(CATALOG['soy_protein_water'], food_id='safe', name='Plain option')
        hidden = replace(safe, food_id='hidden', ingredients=['soy_protein','peanuts'], contains=['soy'], tags=['vegan'])
        unknown = replace(safe, food_id='unknown', ingredients=[], ingredients_complete=False, tags=[])
        flow, client = planner(profile(forbidden_ingredients=['Peanut']), [hidden, unknown, safe])
        await flow.recommend_next_meal(0)
        self.assertEqual([c.food_id for c in client.recommend.call_args.args[0].candidate_foods], ['safe'])
        self.assertFalse(candidate_allowed(hidden, profile(allergens=['nuts']), 0))
        # A legacy partial recipe cannot prove absence of unspecified ingredients.
        self.assertFalse(candidate_allowed(CATALOG['chicken_rice_bowl'], profile(forbidden_ingredients=['garlic']), 0))
        self.assertTrue(candidate_allowed(CATALOG['plain_greek_yogurt'], profile(forbidden_ingredients=['garlic']), 0))

    async def test_preferred_indian_beats_equally_suitable_western_meal(self):
        base = FoodCatalogEntry('base','Plate','1 plate',MacroTotal(460,40,40,12), tags=['vegetarian','light'],
                                ingredients=['tofu','rice','spinach'], ingredients_complete=True)
        western = replace(base, food_id='western', name='A plate', cuisines=['western'])
        indian = replace(base, food_id='indian', name='B plate', cuisines=['indian'])
        flow, client = planner(profile(preferred_cuisines=['Indian']), [western,indian])
        result, prepared = await flow.recommend_next_meal(0)
        scores = {c.food_id:c.fit_score for c in prepared.candidate_foods}
        self.assertAlmostEqual(scores['indian']-scores['western'],8)
        self.assertEqual(result.suggestions[0].candidate_id, 'indian')
        self.assertEqual(set(scores), {'indian','western'}, 'nonpreferred cuisine remains eligible')
        prompt = build_ranking_prompt(client.recommend.call_args.args[0])
        self.assertIn('Indian',prompt)
        self.assertIn('soft preferences',prompt)

    async def test_hard_rules_cannot_be_overridden_by_clock_macro_or_preferences(self):
        safe = CATALOG['soy_protein_water']
        banned = replace(safe, food_id='banned', name='Favourite option', ingredients=['egg','milk'], contains=['egg','dairy'],
                         tags=['ovo_vegetarian','high_protein','late_evening_friendly'], cuisines=['indian'])
        for clock in ('18:30','22:30','23:20'):
            for rules in ({'eggs_allowed':False}, {'dairy_allowed':False}, {'forbidden_foods':['banned']},
                          {'forbidden_foods':['Favourite option']}, {'forbidden_ingredients':['egg']},
                          {'allergens':['egg'], 'eggs_allowed':True}):
                p = profile(preferred_cuisines=['indian'], commonly_eaten_foods=['Favourite option'], **rules)
                flow, client = planner(p,[banned,safe],clock)
                result,_ = await flow.recommend_next_meal(0)
                self.assertNotIn('banned',[c.food_id for c in client.recommend.call_args.args[0].candidate_foods] if client.recommend.called else [])
                self.assertNotIn('banned',[c.candidate_id for c in result.suggestions])

    async def test_soft_avoidance_familiarity_styles_and_variety_do_not_ban(self):
        entry=CATALOG['tofu_clear_soup']
        neutral, _ = planner(profile(),[entry],recent=['Tofu Clear Soup'])
        familiar, _ = planner(profile(commonly_eaten_foods=[entry.food_id],preferred_meal_styles=['light']),[entry],recent=['Tofu Clear Soup'])
        avoiding, _ = planner(profile(avoided_foods=[entry.food_id]),[entry])
        high, _ = planner(profile(variety_preference='high'),[entry],recent=['Tofu Clear Soup'])
        low, _ = planner(profile(variety_preference='low'),[entry],recent=['Tofu Clear Soup'])
        args=(entry, neutral.prepare(0).profile, neutral.prepare(0).remaining, 'tofu clear soup')
        def score(flow):
            return flow._score_entry(entry,flow._profile_store.get(0),args[2],args[3])[0]
        self.assertAlmostEqual(score(familiar)-score(neutral),10)  # familiar +4, style +6
        self.assertLess(score(high), score(neutral))
        self.assertLess(score(neutral), score(low))
        self.assertTrue(candidate_allowed(entry,avoiding._profile_store.get(0),0))
        self.assertTrue(avoiding.prepare(0).candidate_foods)

    async def test_nonvegetarian_does_not_require_meat_and_bans_win_conflicts(self):
        p=profile(diet_type='non-vegetarian')
        self.assertTrue(candidate_allowed(CATALOG['chicken_rice_bowl'],p,0))
        self.assertTrue(candidate_allowed(CATALOG['tofu_clear_soup'],p,0))
        vegan=profile(diet_type='vegan',eggs_allowed=True,dairy_allowed=True)
        self.assertFalse(candidate_allowed(CATALOG['egg_toast_plate'],vegan,0))
        self.assertFalse(candidate_allowed(CATALOG['plain_greek_yogurt'],vegan,0))
        restricted=profile(diet_type='non_vegetarian',restrictions=['vegetarian'])
        self.assertFalse(candidate_allowed(CATALOG['chicken_rice_bowl'],restricted,0))

    async def test_model_cannot_reintroduce_forbidden_candidate(self):
        flow,client=planner(profile(forbidden_foods=['chicken_rice_bowl']),[CATALOG['chicken_rice_bowl'],CATALOG['soy_protein_water']])
        async def malicious(request):
            allowed = request.candidate_foods[0]
            suggestion=replace(RecommendedMeal.from_candidate(allowed,'',''),candidate_id='chicken_rice_bowl')
            return RecommendationResult('',request.today_totals,request.remaining.remaining,[suggestion],'model_ranked')
        client.recommend.side_effect=malicious
        result,_=await flow.recommend_next_meal(0)
        self.assertEqual(result.source,'deterministic_fallback')
        self.assertTrue(all(c.candidate_id!='chicken_rice_bowl' for c in result.suggestions))


class SavedDietaryProfileTests(unittest.TestCase):
    def setUp(self):
        self.table=_FakeTable()
        self.repo=DynamoNutritionRepository(self.table,table_name='fitness',now_fn=lambda:datetime(2026,9,6,12,tzinfo=ZoneInfo('UTC')))
        self.service=NutritionService(self.repo)
        self.identity=self.service.resolve_user(101,'synthetic','Synthetic')
        self.rules={'diet_type':'vegetarian','eggs_allowed':True,'dairy_allowed':False,'allergens':['milk'],
                    'forbidden_ingredients':['peanut'],'forbidden_foods':['egg_toast_plate'],
                    'preferred_cuisines':['indian'],'preferred_staples':['dal'],'preferred_meal_styles':['light'],
                    'commonly_eaten_foods':['tofu'],'avoided_foods':['paneer'],'variety_preference':'high'}

    def test_saved_rules_roundtrip_and_survive_questionnaire_only_update(self):
        response=self.service.save_profile(self.identity,{**BASELINE_PROFILE_PAYLOAD,**self.rules})
        for key,value in self.rules.items(): self.assertEqual(response['profile'][key],value)
        self.service.save_profile(self.identity,{**BASELINE_PROFILE_PAYLOAD,'weight_kg':81})
        saved=self.repo.get_profile(self.identity.user_id)
        for key,value in self.rules.items(): self.assertEqual(getattr(saved,key),value)
        reloaded=UserProfile.from_payload(saved.to_payload())
        self.assertEqual(saved,reloaded)
        with tempfile.TemporaryDirectory() as directory:
            store=UserProfileStore(Path(directory)/'profiles.json')
            store.upsert(saved)
            self.assertEqual(store.get(101),saved)
        prepared=self.service._planner_for_identity(self.identity).prepare(101)
        self.assertTrue(prepared.candidate_foods)
        self.assertTrue(all('dairy_free' in c.tags for c in prepared.candidate_foods))
        self.assertNotIn('egg_toast_plate',[c.food_id for c in prepared.candidate_foods])

    def test_invalid_rules_rejected_before_any_write(self):
        for rule in ({'eggs_allowed':'false'},{'dairy_allowed':1},{'allergens':'milk'},
                     {'forbidden_ingredients':[None]},{'diet_type':'anything'},{'variety_preference':'anything'},
                     {'restrictions':'vegetarian'}):
            before=copy.deepcopy(self.table.items)
            with self.assertRaises(InvalidUserInput):
                self.service.save_profile(self.identity,{**BASELINE_PROFILE_PAYLOAD,**rule})
            self.assertEqual(self.table.items,before)

    def test_legacy_profile_defaults_preserve_diet_and_egg_behavior(self):
        p=UserProfile.from_payload({'telegram_user_id':0,'display_name':'Synthetic','daily_target':MacroTotal(2100,150,220,70).to_payload(),
                                    'restrictions':['vegetarian']})
        self.assertIsNone(p.eggs_allowed)
        self.assertFalse(candidate_allowed(CATALOG['egg_toast_plate'],p,0))
        self.assertTrue(candidate_allowed(CATALOG['plain_greek_yogurt'],p,0))

    def test_legacy_file_identity_migration_preserves_food_rules(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'legacy.json'
            path.write_text(json.dumps({'profiles':[{'person':'pooja','display_name':'Synthetic',
                'daily_target':MacroTotal(2100,150,220,70).to_payload(),**self.rules}]}))
            saved=UserProfileStore(path).list_profiles()[0]
            for key,value in self.rules.items(): self.assertEqual(getattr(saved,key),value)


if __name__=='__main__': unittest.main()
