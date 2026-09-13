"""Regression tests for workout lifecycle races and snapshot reads."""
import copy
import unittest
from unittest.mock import patch

from tests import test_workout_execution as fixtures
from macro_bot.workout_execution import WorkoutConflict


class WorkoutConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkoutExecutionTests()
        self.fixture.setUp()
        self.repo = self.fixture.repo
        self.workouts = self.fixture.service.workout_execution
        self.user = self.fixture.identity()

    def race_before_write(self, concurrent_action, action):
        original = self.repo.store.transact_write
        fired = False

        def intercept(operations):
            nonlocal fired
            if not fired:
                fired = True
                concurrent_action()
            return original(operations)

        with patch.object(self.repo.store, 'transact_write', side_effect=intercept):
            with self.assertRaises(WorkoutConflict):
                action()
        self.assertTrue(fired)

    def test_cancel_prevents_inflight_child_mutations(self):
        for action_name in ('save', 'skip_set', 'select', 'skip', 'reset'):
            with self.subTest(action=action_name):
                session = self.fixture.start(day='PUSH')
                sid = session['session']['session_id']
                exercise = next(e for e in session['executions'] if len(e['allowed_exercise_ids']) > 1)
                eid = exercise['execution_id']
                revision = exercise['revision']
                alternative = next(x for x in exercise['allowed_exercise_ids'] if x != exercise['performed_exercise_id'])
                actions = {
                    'save': lambda: self.workouts.put_set(self.user, sid, eid, 1, {'load_value': 20, 'reps': 8, 'execution_expected_revision': revision}),
                    'skip_set': lambda: self.workouts.skip_set(self.user, sid, eid, 1, {'execution_expected_revision': revision}),
                    'select': lambda: self.workouts.select_exercise(self.user, sid, eid, {'performed_exercise_id': alternative, 'expected_revision': revision}),
                    'skip': lambda: self.workouts.skip_execution(self.user, sid, eid, {'expected_revision': revision}),
                    'reset': lambda: self.workouts.reset_execution(self.user, sid, eid, {'expected_revision': revision}),
                }
                before = copy.deepcopy(exercise)
                self.race_before_write(
                    lambda: self.workouts.cancel_session(self.user, sid, {'expected_revision': 1}),
                    actions[action_name],
                )
                result = self.workouts.get_session(self.user, sid)
                self.assertEqual(result['session']['status'], 'cancelled')
                after = next(e for e in result['executions'] if e['execution_id'] == eid)
                self.assertEqual({k: after[k] for k in before}, before)

    def test_completion_rejects_concurrent_reset_of_skipped_exercise(self):
        session = self.fixture.start()
        sid = session['session']['session_id']
        for exercise in session['executions']:
            self.workouts.skip_execution(self.user, sid, exercise['execution_id'], {'expected_revision': 1})
        self.race_before_write(
            lambda: self.workouts.reset_execution(self.user, sid, session['executions'][0]['execution_id'], {'expected_revision': 2}),
            lambda: self.workouts.complete_session(self.user, sid, {'expected_revision': 1}),
        )
        result = self.workouts.get_active_session(self.user)
        self.assertEqual(result['session']['status'], 'in_progress')
        self.assertEqual(result['executions'][0]['status'], 'pending')

    def test_completed_session_rejects_inflight_reset(self):
        session = self.fixture.start()
        sid = session['session']['session_id']
        for exercise in session['executions']:
            self.workouts.skip_execution(self.user, sid, exercise['execution_id'], {'expected_revision': 1})
        self.race_before_write(
            lambda: self.workouts.complete_session(self.user, sid, {'expected_revision': 1}),
            lambda: self.workouts.reset_execution(self.user, sid, session['executions'][0]['execution_id'], {'expected_revision': 2}),
        )
        result = self.workouts.get_session(self.user, sid)
        self.assertEqual(result['session']['status'], 'completed')
        self.assertTrue(all(e['status'] == 'skipped' for e in result['executions']))

    def test_start_uses_one_programme_snapshot_during_publication(self):
        original = self.fixture.service.programme_repository.get_programme
        initial = original()

        def publish_after_read(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            self.repo.publish_core_options_programme()
            return snapshot

        with patch.object(self.workouts, 'programme_reader', side_effect=publish_after_read) as reads:
            result = self.fixture.start(day='PUSH')
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(result['session']['programme_version_id'], initial['version']['version_id'])
        day = next(d for d in initial['days'] if d['day_code'] == 'PUSH')
        for execution, prescription in zip(result['executions'], day['prescriptions']):
            self.assertEqual(execution['allowed_exercise_ids'], prescription['allowed_exercise_ids'])
        self.assertNotEqual(original()['version']['version_id'], initial['version']['version_id'])

    def test_workout_reads_request_consistency_after_save(self):
        session = self.fixture.start()
        sid = session['session']['session_id']
        eid = session['executions'][0]['execution_id']
        with patch.object(self.repo.store, 'query', wraps=self.repo.store.query) as queries:
            result = self.workouts.put_set(self.user, sid, eid, 1, {'load_value': 20, 'reps': 8})
        self.assertTrue(queries.call_args_list)
        self.assertTrue(all(call.kwargs.get('ConsistentRead') is True for call in queries.call_args_list))
        self.assertEqual(result['executions'][0]['sets'][0]['reps'], 8)

    def test_nested_set_response_preserves_legacy_metadata(self):
        session = self.fixture.start()
        sid = session['session']['session_id']
        eid = session['executions'][0]['execution_id']
        result = self.workouts.put_set(self.user, sid, eid, 1, {'load_value': 20.5, 'reps': 8})
        expected = next(item for item in self.fixture.table.items.values()
                        if item.get('entity_type') == 'workout_set')
        saved = result['executions'][0]['sets'][0]
        self.assertEqual({key: saved[key] for key in ('PK', 'SK', 'entity_type')},
                         {key: expected[key] for key in ('PK', 'SK', 'entity_type')})
        self.assertEqual(saved['load_value'], 20.5)
        for record in (result['session'], result['executions'][0]):
            self.assertFalse({'PK', 'SK', 'entity_type'} & record.keys())
