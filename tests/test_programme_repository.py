"""Programme persistence and composition without nutrition ownership."""
import ast
import copy
import io
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from macro_bot.data_errors import DataError
from macro_bot.dynamo_store import DynamoDBStore
from macro_bot.dynamo_programme_repository import DynamoProgrammeRepository
from macro_bot.dynamo_workout_repository import DynamoWorkoutRepository
from macro_bot.programme_repository import ProgrammeRepository, ProgrammeSeedConflict
from macro_bot.serverless_data import DynamoNutritionRepository, ProgrammeSeedConflict as LegacyConflict
from macro_bot.serverless_service import NutritionService, build_service
from macro_bot.workout_service import WorkoutService
from macro_bot.workout_types import InvalidWorkoutInput
from macro_bot.workout_programme import (
    INITIAL_VERSION_ID, CORE_OPTIONS_VERSION_ID, PROGRAMME_PK,
    initial_programme_records, core_options_programme_records,
)
from tests.test_serverless_data import _FakeTable, TransactionCanceledException


class ProgrammeRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.table = _FakeTable()
        self.now = datetime(2026, 9, 13, 12, 34, 56, 123456, tzinfo=timezone.utc)
        self.store = DynamoDBStore(self.table, self.table.name, self.table.meta.client)
        self.programmes: ProgrammeRepository = DynamoProgrammeRepository(self.store, now_fn=lambda: self.now)

    def test_current_historical_and_missing_reads_preserve_schema(self):
        self.assertIsNone(self.programmes.get_programme())
        self.assertIsNone(self.programmes.get_programme_day('PULL'))
        self.programmes.seed_workout_programme()
        expected = {(item['PK'], item['SK']): item for item in initial_programme_records()}
        self.assertEqual(self.table.items, expected)
        initial = self.programmes.get_programme()
        self.assertEqual(initial['version']['version_id'], INITIAL_VERSION_ID)
        self.assertEqual(self.programmes.get_programme_day('pull')['day']['day_code'], 'PULL')
        self.programmes.publish_core_options_programme()
        self.assertEqual(self.programmes.get_programme()['version']['version_id'], CORE_OPTIONS_VERSION_ID)
        historical = self.programmes.get_programme(INITIAL_VERSION_ID)
        self.assertEqual(historical['version'], initial['version'])
        self.assertEqual(historical['days'], initial['days'])
        self.assertIsNone(self.programmes.get_programme('missing'))
        self.assertIsNone(self.programmes.get_programme_day('missing'))
        for item in core_options_programme_records():
            self.assertEqual(self.table.items[(item['PK'], item['SK'])], item)
        for sk in ('META', 'ACTIVE'):
            self.assertEqual(self.table.items[(PROGRAMME_PK, sk)]['updated_at'], '2026-09-13T12:34:56Z')

    def test_publication_keeps_pointer_conditions_and_additions_in_one_transaction(self):
        self.programmes.seed_workout_programme()
        with patch.object(self.store, 'transact_write', wraps=self.store.transact_write) as writes:
            self.programmes.publish_core_options_programme()
        writes.assert_called_once()
        operations = writes.call_args.args[0]
        pointers = [op for op in operations if op['Item']['SK'] in ('META', 'ACTIVE')]
        self.assertEqual(len(pointers), 2)
        for op in pointers:
            self.assertEqual(op['ConditionExpression'], 'active_version_id = :version')
            self.assertEqual(op['ExpressionAttributeValues'], {':version': INITIAL_VERSION_ID})
        self.assertEqual(len(operations), len(core_options_programme_records()) + 2)
        with patch.object(self.store, 'transact_write', side_effect=AssertionError('Idempotent publish must not write')):
            self.assertEqual(self.programmes.publish_core_options_programme()['created'], 0)

    def test_publication_failure_preserves_records_and_public_error_semantics(self):
        self.programmes.seed_workout_programme()
        before = copy.deepcopy(self.table.items)
        with patch.object(self.store, 'transact_write', side_effect=TransactionCanceledException()):
            with self.assertRaisesRegex(ProgrammeSeedConflict, 'changed during publication'):
                self.programmes.publish_core_options_programme()
        self.assertEqual(self.table.items, before)
        self.assertIs(LegacyConflict, ProgrammeSeedConflict)
        self.assertTrue(issubclass(ProgrammeSeedConflict, DataError))
        failure = RuntimeError('connection unavailable')
        with patch.object(self.store, 'transact_write', side_effect=failure):
            with self.assertRaises(RuntimeError) as raised:
                self.programmes.publish_core_options_programme()
        self.assertIs(raised.exception, failure)

    def test_seed_handles_matching_and_conflicting_concurrent_inserts(self):
        original = self.store.put_item
        first = initial_programme_records()[0]
        key = (first['PK'], first['SK'])
        def concurrent_insert(item, **kwargs):
            if (item['PK'], item['SK']) == key:
                self.table.items[key] = copy.deepcopy(item)
            return original(item, **kwargs)
        with patch.object(self.store, 'put_item', side_effect=concurrent_insert):
            result = self.programmes.seed_workout_programme()
        self.assertEqual(result['existing'], 1)
        self.assertEqual(result['created'], len(initial_programme_records()) - 1)
        self.table.items.clear()
        def conflicting_insert(item, **kwargs):
            self.table.items[(item['PK'], item['SK'])] = {**item, 'conflicting': True}
            return original(item, **kwargs)
        with patch.object(self.store, 'put_item', side_effect=conflicting_insert):
            with self.assertRaisesRegex(ProgrammeSeedConflict, 'after concurrent write'):
                self.programmes.seed_workout_programme()
        self.assertEqual(len(self.table.items), 1)

    def test_existing_workout_and_substitution_keep_saved_programme_version(self):
        self.programmes.seed_workout_programme()
        workout = WorkoutService(DynamoWorkoutRepository(self.store), self.programmes.get_programme,
                                 now_fn=lambda: self.now, session_id_factory=lambda: 'session')
        identity = SimpleNamespace(user_id='owner', pk='USER#owner')
        started = workout.start_session(identity, 'PUSH', actual_local_date='2026-09-13')
        self.programmes.publish_core_options_programme()
        self.assertEqual(workout.get_active_session(identity), started)
        core = started['executions'][-1]
        with patch.object(workout, 'programme_reader', wraps=self.programmes.get_programme) as reads:
            selected = workout.select_exercise(identity, 'session', core['execution_id'],
                {'performed_exercise_id': 'dead_bug', 'expected_revision': 1})
        reads.assert_called_once_with(version_id=INITIAL_VERSION_ID)
        self.assertEqual(selected['session']['programme_version_id'], INITIAL_VERSION_ID)
        self.assertEqual(selected['executions'][-1]['execution_type'], 'bodyweight_reps')
        with self.assertRaises(InvalidWorkoutInput):
            workout.select_exercise(identity, 'session', core['execution_id'],
                {'performed_exercise_id': 'russian_twist', 'expected_revision': 2})
        workout.cancel_session(identity, 'session', {'expected_revision': 1})
        with patch.object(workout, 'programme_reader', wraps=self.programmes.get_programme) as reads:
            workout.get_session(identity, 'session')
        reads.assert_called_once_with(version_id=INITIAL_VERSION_ID)
        workout.session_id_factory = lambda: 'new-session'
        new = workout.start_session(identity, 'PUSH', actual_local_date='2026-09-13')
        self.assertEqual(new['session']['programme_version_id'], CORE_OPTIONS_VERSION_ID)

    def test_programme_storage_and_composed_workout_bypass_nutrition_helpers(self):
        nutrition = DynamoNutritionRepository(self.table, table_name=self.table.name, now_fn=lambda: self.now)
        facade = build_service(nutrition)
        with ExitStack() as stack:
            for name in ('_get', '_query', '_transact_write', 'get_workout_programme', 'get_workout_programme_day'):
                stack.enter_context(patch.object(nutrition, name, side_effect=AssertionError('Nutrition dependency')))
            facade.programme_repository.seed_workout_programme()
            self.assertEqual(facade.workout_programme()['version']['version_id'], INITIAL_VERSION_ID)
            facade.programme_repository.publish_core_options_programme()
            result = facade.workout_execution.start_session(SimpleNamespace(user_id='owner', pk='USER#owner'),
                'PUSH', actual_local_date='2026-09-13')
        self.assertEqual(result['session']['programme_version_id'], CORE_OPTIONS_VERSION_ID)
        self.assertIs(facade.workout_execution.programme_reader.__self__, facade.programme_repository)

    def test_facade_accepts_independently_injected_dependencies(self):
        nutrition = DynamoNutritionRepository(self.table, table_name=self.table.name)
        programmes = Mock(spec_set=ProgrammeRepository)
        workouts = Mock(spec_set=WorkoutService)
        facade = NutritionService(nutrition, programme_repository=programmes, workout_service=workouts)
        programmes.get_programme.return_value = {'version': {'version_id': 'injected'}}
        self.assertEqual(facade.workout_programme('injected'), programmes.get_programme.return_value)
        programmes.get_programme.assert_called_once_with(version_id='injected')
        facade.workout_programme_day('PUSH', 'injected')
        programmes.get_programme_day.assert_called_once_with('PUSH', version_id='injected')
        identity = SimpleNamespace(user_id='owner')
        facade.active_workout(identity)
        workouts.get_active_session.assert_called_once_with(identity)
        self.assertEqual(self.table.items, {})

    def test_legacy_delegators_match_new_repository(self):
        nutrition = DynamoNutritionRepository(self.table, table_name=self.table.name, now_fn=lambda: self.now)
        self.assertEqual(nutrition.seed_workout_programme(dry_run=True), self.programmes.seed_workout_programme(dry_run=True))
        nutrition.seed_workout_programme()
        self.assertEqual(nutrition.get_workout_programme(), self.programmes.get_programme())
        self.assertEqual(nutrition.get_workout_programme_day('push'), self.programmes.get_programme_day('push'))
        nutrition.publish_core_options_programme()
        self.assertEqual(nutrition.get_workout_programme(INITIAL_VERSION_ID), self.programmes.get_programme(INITIAL_VERSION_ID))
        self.assertEqual(nutrition.publish_core_options_programme(), self.programmes.publish_core_options_programme())

    def test_lambda_factories_use_explicit_composition(self):
        from lambda_handlers import api, worker
        nutrition = DynamoNutritionRepository(self.table, table_name=self.table.name)
        with patch.object(worker, '_repository', return_value=nutrition):
            worker_facade = worker._service()
        self.assertIsInstance(worker_facade.programme_repository, DynamoProgrammeRepository)
        resource = Mock()
        resource.Table.return_value = self.table
        with patch.dict('os.environ', {'FITNESS_DATA_TABLE': self.table.name}), \
                patch('boto3.resource', return_value=resource), \
                patch('boto3.client', return_value=self.table.meta.client):
            api_facade = api._service()
        self.assertIsInstance(api_facade.programme_repository, DynamoProgrammeRepository)
        self.assertIs(api_facade.workout_execution.programme_reader.__self__, api_facade.programme_repository)

    def test_seed_cli_uses_programme_repository_without_nutrition(self):
        from scripts import seed_workout_programme
        session = Mock()
        session.resource.return_value.Table.return_value = self.table
        session.client.return_value = self.table.meta.client
        with patch('boto3.Session', return_value=session), redirect_stdout(io.StringIO()):
            self.assertEqual(seed_workout_programme.main(['--table-name', self.table.name, '--dry-run']), 0)
            self.assertEqual(self.table.items, {})
            self.assertEqual(seed_workout_programme.main(['--table-name', self.table.name]), 0)
            self.assertEqual(seed_workout_programme.main(['--table-name', self.table.name, '--core-options']), 0)
        self.assertEqual(self.programmes.get_programme()['version']['version_id'], CORE_OPTIONS_VERSION_ID)

    def test_programme_boundary_has_no_nutrition_dependency(self):
        root = Path(__file__).resolve().parents[1]
        for filename in ('dynamo_programme_repository.py', 'programme_repository.py'):
            tree = ast.parse((root / 'macro_bot' / filename).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, ('serverless_data', 'serverless_service'))
        tree = ast.parse((root / 'macro_bot/serverless_data.py').read_text())
        nutrition = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DynamoNutritionRepository')
        for method in nutrition.body:
            if isinstance(method, ast.FunctionDef) and 'programme' in method.name:
                self.assertEqual(len(method.body), 1, method.name)
                self.assertIsInstance(method.body[0], ast.Return)
                self.assertIn('DynamoProgrammeRepository', ast.unparse(method.body[0]))
