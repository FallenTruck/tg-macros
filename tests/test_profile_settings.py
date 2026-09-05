"""Profile settings validation, isolation and target-preserving persistence."""
import copy
import os
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from lambda_handlers import api
from macro_bot.models import UserProfile
from macro_bot.serverless_data import DynamoNutritionRepository
from macro_bot.serverless_service import NutritionService, InvalidUserInput
from scripts.e2e_support import BASELINE_PROFILE_PAYLOAD
from tests.test_serverless_data import _FakeTable


class ProfileSettingsTests(unittest.TestCase):
    def setUp(self):
        self.table = _FakeTable()
        self.repo = DynamoNutritionRepository(self.table, table_name='fitness', now_fn=lambda: datetime(2026, 9, 6, tzinfo=timezone.utc))
        self.service = NutritionService(self.repo)
        self.identity = self.service.resolve_user(101, 'test', 'Test')
        self.service.save_profile(self.identity, BASELINE_PROFILE_PAYLOAD)
        self.rules = {'diet_type': 'vegetarian', 'eggs_allowed': True, 'dairy_allowed': False,
                      'allergens': ['milk'], 'forbidden_ingredients': ['peanut'], 'forbidden_foods': ['egg_toast_plate'],
                      'restrictions': ['dairy_free'], 'preferred_cuisines': ['Indian'], 'preferred_staples': ['rice'],
                      'preferred_meal_styles': ['light'], 'commonly_eaten_foods': ['tofu'], 'avoided_foods': ['paneer'],
                      'variety_preference': 'high', 'recommendation_bedtime': '22:45'}

    def test_settings_only_save_does_not_append_or_change_targets(self):
        targets = copy.deepcopy(self.repo.list_targets(self.identity.user_id))
        old = self.repo.get_profile(self.identity.user_id)
        self.service.save_profile(self.identity, self.rules)
        self.assertEqual(targets, self.repo.list_targets(self.identity.user_id))
        saved = self.repo.get_profile(self.identity.user_id)
        self.assertEqual(old.daily_target, saved.daily_target)
        self.assertEqual(old.questionnaire_answers, saved.questionnaire_answers)
        self.assertEqual(UserProfile.from_payload(saved.to_payload()), saved)
        self.service.save_profile(self.identity, {**BASELINE_PROFILE_PAYLOAD, 'weight_kg': 81})
        self.service.save_profile(self.identity, {'preferred_cuisines': ['Indian', 'Asian']})
        saved = self.repo.get_profile(self.identity.user_id)
        for key, value in self.rules.items():
            self.assertEqual(getattr(saved, key), ['Indian', 'Asian'] if key == 'preferred_cuisines' else value)

    def test_default_and_invalid_bedtime_before_writes(self):
        saved = self.repo.get_profile(self.identity.user_id)
        payload = saved.to_payload()
        del payload['recommendation_bedtime']
        self.assertEqual(UserProfile.from_payload(payload).recommendation_bedtime, '23:30')
        for value in ('24:00', '22:60', '7:30', '22:45Z', '2026-09-06T22:45', None, 2245, ''):
            before = copy.deepcopy(self.table.items)
            with self.assertRaises(InvalidUserInput):
                self.service.save_profile(self.identity, {'recommendation_bedtime': value})
            self.assertEqual(before, self.table.items)
        self.assertEqual(replace(saved, recommendation_bedtime='00:30').recommendation_bedtime, '00:30')

    def test_authenticated_api_rejects_body_identity_and_preserves_other_users(self):
        other = self.service.resolve_user(202, 'other', 'Other')
        self.service.save_profile(other, BASELINE_PROFILE_PAYLOAD)
        before = copy.deepcopy({key: value for key, value in self.table.items.items() if key[0] == other.pk})
        token, _ = self.repo.create_browser_session(self.identity)
        with patch.dict(os.environ, {'MINI_APP_URL': 'https://testserver'}), patch.object(api, '_service', return_value=self.service), TestClient(api.app, base_url='https://testserver') as client:
            client.cookies.set('jf_session', token)
            response = client.post('/api/profile', json={**self.rules, 'user_id': other.user_id, 'telegram_user_id': 202}, headers={'Origin': 'https://testserver'})
            self.assertEqual(response.status_code, 400)
            response = client.post('/api/profile', json=self.rules, headers={'Origin': 'https://testserver'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['viewer']['telegram_user_id'], 101)
            self.assertEqual(client.get('/api/profile').json()['profile']['recommendation_bedtime'], '22:45')
            response = client.post('/api/targets', json={**BASELINE_PROFILE_PAYLOAD, 'weight_kg': 81}, headers={'Origin': 'https://testserver'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get('/api/profile').json()['profile']['forbidden_ingredients'], ['peanut'])
        self.assertEqual(before, {key: value for key, value in self.table.items.items() if key[0] == other.pk})
