"""Exercise the production Telegram callback against durable repository operations."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lambda_handlers import worker
from macro_bot.models import MacroTotal, MealEstimate, UserProfile, RecommendationResult
from macro_bot.serverless_data import DynamoNutritionRepository
from macro_bot.serverless_service import NutritionService
from macro_bot.formatting import format_recommendation_message, format_nutrition_state_message
from tests.test_serverless_data import _FakeTable
from tests.test_serverless_adapters import _FakeBot


class PostLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 6, 10, 30, tzinfo=timezone.utc)
        self.table = _FakeTable()
        self.repo = DynamoNutritionRepository(self.table, table_name='fitness', now_fn=lambda: self.now)
        self.service = NutritionService(self.repo)
        self.identity = self.service.resolve_user(101, 'synthetic', 'Synthetic')
        self.repo.save_profile(self.identity, UserProfile(101, 'synthetic', 'Synthetic', MacroTotal(2100, 150, 220, 70)))
        self.service._planner._recommendation_client = None
        self.bot = _FakeBot(b'')
        previous = self.pending(780, 43, 75, 28)
        self.repo.finalize_action(self.identity, previous.token, 'confirm')
        self.action = self.pending(640, 43, 70, 20)

    def pending(self, kcal, protein, carbs, fat, *, eaten_at=None):
        return self.repo.create_pending_meal(self.identity, chat_id=99, request_message_id=20,
            caption='synthetic meal', estimate=MealEstimate('Synthetic meal', kcal, protein, carbs, fat, 1.0, ''),
            eaten_at=eaten_at or self.now)

    async def confirm(self):
        await worker.process_update_message({'update_id': 10, 'payload': {'callback_query': {
            'id': 'callback', 'from': {'id': 101}, 'data': f'meal:v1:confirm:{self.action.token}',
            'message': {'message_id': 500, 'chat': {'id': 99, 'type': 'private'}}}}},
            service=self.service, bot=self.bot)

    async def test_persist_rebuild_send_state_then_recommend_and_duplicate_receipt(self):
        real = self.service.recommendation_async
        async def recommend(identity):
            self.assertEqual(self.repo.get_meal(identity, self.action.meal_id).status, 'confirmed')
            self.assertEqual(len(self.bot.sent), 1)
            self.assertIn('1,420 / 2,100 kcal', self.bot.sent[0]['text'])
            return await real(identity)
        self.service.recommendation_async = recommend
        with self.assertLogs('lambda_handlers.worker', level='INFO') as logs:
            await self.confirm()
        self.assertEqual(len(self.bot.sent), 2)
        first, second = [entry['text'] for entry in self.bot.sent]
        for value in ('✅ Meal logged', 'This meal\n640 kcal', 'P 43g · C 70g · F 20g', 'Today',
                      'Protein\n86 / 150g', 'Carbs\n145 / 220g', 'Fat\n48 / 70g', 'Remaining\n680 kcal'):
            self.assertIn(value, first)
        self.assertTrue(second.startswith('🥗 What to eat next'))
        self.assertNotIn('1,420', second)
        self.assertNotIn('Remaining\n', second)
        for message in (first, second):
            self.assertLess(len(message.encode('utf-16-le')) // 2, 4096)
        for event in ('message1_sent', 'recommendation_started', 'recommendation_fallback', 'message2_sent'):
            self.assertIn(event, ' '.join(logs.output))
        self.assertNotIn('synthetic meal', ' '.join(logs.output))
        await self.confirm()
        self.assertEqual(len(self.bot.sent), 2)
        self.assertEqual(self.repo.daily_summary(self.identity, self.now.date()).meal_count, 2)

    async def test_state_uses_final_confirmed_macros_not_action_estimate_or_pending_food(self):
        self.pending(900, 90, 90, 30)  # Unconfirmed food must not enter daily totals.
        result = self.repo.finalize_action(self.identity, self.action.token, 'confirm')
        result.action.estimate = MealEstimate('Stale estimate', 9999, 999, 999, 999, 1, '')
        with patch.object(self.service, 'finalize_action', return_value=result):
            await self.confirm()
        self.assertIn('1,420 / 2,100', self.bot.sent[0]['text'])
        self.assertNotIn('9999', self.bot.sent[0]['text'])

    async def test_skipped_and_generation_failure_leave_state_delivered(self):
        for outcome in ('skipped', 'failure', 'preparation'):
            with self.subTest(outcome=outcome):
                self.setUp()
                if outcome == 'failure':
                    self.service.recommendation_async = AsyncMock(side_effect=RuntimeError('private meal detail'))
                elif outcome == 'preparation':
                    self.service.should_recommend_after_meal = lambda *a: (_ for _ in ()).throw(RuntimeError('private detail'))
                else:
                    self.service.recommendation_async = AsyncMock(return_value=(RecommendationResult('', MacroTotal(0,0,0,0), MacroTotal(0,0,0,0), [], 'skipped'), None))
                await self.confirm()
                self.assertEqual(len(self.bot.sent), 1)
                self.assertEqual(self.repo.get_meal(self.identity, self.action.meal_id).status, 'confirmed')

    async def test_recommendation_formatting_send_and_cleanup_failure_are_isolated(self):
        for failure in ('format', 'send', 'cleanup', 'ack'):
            with self.subTest(failure=failure):
                self.setUp()
                send = self.bot.send_message
                async def send_with_failure(**kwargs):
                    if kwargs['text'].startswith('🥗'):
                        raise RuntimeError('private Telegram error')
                    return await send(**kwargs)
                if failure == 'send': self.bot.send_message = send_with_failure
                if failure == 'cleanup': self.bot.edit_message_text = AsyncMock(side_effect=RuntimeError())
                if failure == 'ack': self.bot.answer_callback_query = AsyncMock(side_effect=RuntimeError())
                with patch.object(worker, 'format_recommendation_message', side_effect=RuntimeError() if failure == 'format' else format_recommendation_message):
                    await self.confirm()
                self.assertTrue(self.bot.sent[0]['text'].startswith('✅ Meal logged'))
                self.assertEqual(self.repo.get_meal(self.identity, self.action.meal_id).status, 'confirmed')
                count = len(self.bot.sent)
                await self.confirm()
                self.assertEqual(len(self.bot.sent), count)

    async def test_failed_message1_retry_delivers_state_before_recommendation(self):
        send = self.bot.send_message
        self.bot.send_message = AsyncMock(side_effect=RuntimeError('temporary transport failure'))
        self.service.recommendation_async = AsyncMock()
        with self.assertRaises(RuntimeError):
            await self.confirm()
        self.service.recommendation_async.assert_not_called()
        self.assertEqual(self.repo.get_meal(self.identity, self.action.meal_id).status, 'confirmed')
        self.bot.send_message = send
        del self.service.recommendation_async
        await self.confirm()
        self.assertEqual(len(self.bot.sent), 2)

    async def test_historical_log_confirms_its_day_and_suppresses_recommendation(self):
        self.action = self.pending(640, 43, 70, 20, eaten_at=self.now - timedelta(days=1))
        self.service.recommendation_async = AsyncMock()
        await self.confirm()
        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn('Logged day · 2026-09-05', self.bot.sent[0]['text'])
        self.assertNotIn('\nToday\n', self.bot.sent[0]['text'])
        self.service.recommendation_async.assert_not_called()

    async def test_same_day_breakfast_backfill_message_and_current_time_recommendation(self):
        self.now = self.now.replace(hour=14, minute=0)  # 22:00 Singapore
        self.action = self.pending(420, 24, 46, 15, eaten_at=self.now.replace(hour=0, minute=30))
        captured = []
        real = self.service.recommendation_async
        async def recommend(identity):
            result, prepared = await real(identity)
            captured.append(prepared)
            return result, prepared
        self.service.recommendation_async = recommend
        await self.confirm()
        self.assertTrue(self.bot.sent[0]['text'].startswith('✅ Meal logged for 8:30 AM'))
        self.assertEqual(captured[0].timing.local_datetime[11:16], '22:00')
        self.assertEqual(captured[0].timing.most_recent_meal_time[11:16], '18:30')
        self.assertEqual(self.repo.get_meal(self.identity, self.action.meal_id).entry_delay_minutes, 810)

    async def test_auto_confirm_sweep_uses_same_state_and_fast_fallback(self):
        self.now += timedelta(hours=2)
        finalized = self.repo.auto_confirm_expired_action(self.identity, self.action.token)
        with patch.object(worker, '_service', return_value=self.service), patch.object(worker, '_bot', return_value=self.bot), patch.object(self.service, 'expire_pending_actions', return_value=[finalized.action]):
            count = await worker._expire_meal_actions()
        self.assertEqual(count, 1)
        self.assertEqual(len(self.bot.sent), 2)
        self.assertIn('Meal logged for 6:30 PM', self.bot.sent[0]['text'])
        self.assertIn('automatically after timeout', self.bot.sent[0]['text'])
        self.assertTrue(self.bot.sent[1]['text'].startswith('🥗'))

    async def test_message_lengths_with_long_model_explanations(self):
        self.repo.finalize_action(self.identity, self.action.token, 'confirm')
        result, _ = self.service.recommendation(self.identity)
        from dataclasses import replace
        result = replace(result, summary='x' * 5000, suggestions=[replace(result.suggestions[0], name='x'*1000,
                         serving='x'*1000, fit_rationale='x'*1000, tradeoffs='x'*1000)] * 3)
        self.assertLess(len(format_recommendation_message(result)), 4096)
        emoji = replace(result, summary='🥗'*5000, suggestions=[replace(result.suggestions[0], name='🥗'*1000,
                         serving='🥗'*1000, fit_rationale='🥗'*1000, tradeoffs='🥗'*1000)] * 3)
        self.assertLessEqual(len(format_recommendation_message(emoji).encode('utf-16-le')) // 2, 4000)


if __name__ == '__main__':
    unittest.main()
