#!/usr/bin/env python3
"""Run five fixed deployed retrospective scenarios only as javaan-e2e."""
import argparse
import json
import sys
from pathlib import Path

from botocore.config import Config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.e2e_support import (E2E_USERNAME, DEV_STACK_NAME, aws_session, dev_resources,
                                 read_e2e_records, validate_e2e_credential)
from scripts.reset_e2e_account import reset_e2e_account
from macro_bot.recommendation_scenarios import SCENARIOS


def validate_scenario(result):
    name = result['scenario']
    assert result['meal_status'] == 'confirmed'
    assert result['confirmed_at']
    if name == 'previous_day':
        assert result['source'] == 'skipped' and not result['recommendation_allowed']
        assert not result['suggestions'] and not result['preview']
        assert result['logged_day'] != result['today']
        assert result['logged_day_consumed']['calories'] == 420
        assert result['current_day_consumed']['calories'] == 0
        return
    assert result['recommendation_allowed'] and result['suggestions']
    assert result['source'] in ('model_ranked', 'deterministic_fallback')
    candidates = {item['food_id']: item for item in result['candidates']}
    assert set(result['suggestions']) <= candidates.keys()
    first = candidates[result['suggestions'][0]]
    timing = result['timing']
    if name == 'current_meal':
        assert result['entry_delay_minutes'] == 0 and result['eaten_at'] == result['confirmed_at']
        assert timing['minutes_until_bedtime'] == 300 and not timing['possible_incomplete_day']
        assert result['state_preview'].startswith('✅ Meal logged\n')
    elif name == 'same_day_backfill':
        assert [item['local_time'][11:16] for item in result['today_meals']] == ['08:30', '13:00', '19:15']
        assert timing['local_datetime'][11:16] == '22:00' and timing['most_recent_meal_time'][11:16] == '19:15'
        assert timing['minutes_until_bedtime'] == 90 and not timing['possible_incomplete_day']
        assert result['logged_day_consumed']['calories'] == 1660
        assert result['entry_delay_minutes'] == 810
        assert result['state_preview'].startswith('✅ Meal logged for 8:30 AM')
    elif name == 'incomplete_backfill':
        assert timing['possible_incomplete_day']
        assert "Based on what you've logged today" in result['preview']
        assert "If today's log is complete" in result['preview']
        assert first['meal_type'] != 'full_meal' and first['calories'] <= 450 and first['protein_g'] >= 20
    elif name == 'custom_bedtime':
        assert timing['minutes_until_bedtime'] == 45 and timing['band'] == 'top_up'
        assert '22:45' in result['preview'] and '23:30' not in result['preview']
        assert first['calories'] <= 250


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('Pass --live for four real recommendation calls and synthetic fixture writes/resets.')
    session, table, _, repo = dev_resources(aws_session())
    read_e2e_records(table)
    validate_e2e_credential(repo.get_web_credential(E2E_USERNAME))
    resource = session.client('cloudformation').describe_stack_resource(StackName=DEV_STACK_NAME, LogicalResourceId='NutritionLabFunction')
    function = resource['StackResourceDetail']['PhysicalResourceId']
    invoker = session.client('lambda', config=Config(read_timeout=135, retries={'max_attempts': 0}))
    results = []
    try:
        for name in SCENARIOS:
            reset_e2e_account(table=table, repository=repo)
            response = invoker.invoke(FunctionName=function, InvocationType='RequestResponse',
                Payload=json.dumps({'operation': 'retrospective_scenario', 'scenario': name}).encode())
            result = json.loads(response['Payload'].read())
            if response.get('FunctionError'):
                raise RuntimeError('Retrospective scenario failed; inspect sanitized Lambda telemetry.')
            validate_scenario(result)
            results.append(result)
            print(f"{name}: PASS; {result['source']}", flush=True)
    finally:
        reset_e2e_account(table=table, repository=repo)
    output = Path('artifacts/e2e/retrospective-scenarios.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'scenarios': results, 'baseline_restored': True}, indent=2) + '\n')
    print(f'Five scenarios passed; synthetic baseline restored. Report: {output}')


if __name__ == '__main__':
    main()
