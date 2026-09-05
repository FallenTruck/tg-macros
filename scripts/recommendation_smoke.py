#!/usr/bin/env python3
"""Run the deployed dev-only early/late recommendation scenarios as javaan-e2e."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.e2e_support import (E2E_USER_ID, E2E_USERNAME, DEV_STACK_NAME, aws_session, dev_resources,
                                 read_e2e_records, validate_e2e_credential, user_partition_items)
from scripts.reset_e2e_account import reset_e2e_account


def partition_hash(table):
    items = sorted(user_partition_items(table, E2E_USER_ID), key=lambda item: item['SK'])
    return hashlib.sha256(json.dumps(items, sort_keys=True, default=str).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Opt into two real recommendation calls and synthetic baseline reset')
    args = parser.parse_args()
    if not args.live:
        parser.error('Pass --live to run the synthetic deployed smoke (two model calls).')
    session, table, _, repo = dev_resources(aws_session())
    read_e2e_records(table)
    validate_e2e_credential(repo.get_web_credential(E2E_USERNAME))
    resource = session.client('cloudformation').describe_stack_resource(StackName=DEV_STACK_NAME, LogicalResourceId='NutritionLabFunction')
    function_name = resource['StackResourceDetail']['PhysicalResourceId']
    reset_e2e_account(table=table, repository=repo)
    before = partition_hash(table)
    try:
        response = session.client('lambda').invoke(FunctionName=function_name, InvocationType='RequestResponse',
                                                    Payload=json.dumps({'operation': 'recommendation_scenarios'}).encode())
        result = json.loads(response['Payload'].read())
        if response.get('FunctionError'):
            raise RuntimeError('Deployed recommendation scenario invocation failed; inspect sanitized Lambda telemetry.')
        assert partition_hash(table) == before, 'Scenario changed the synthetic domain partition'
        scenarios = result['scenarios']
        assert len(scenarios) == 2
        for scenario in scenarios:
            assert scenario['strategy_version'] == 'nutrition-recommendation-v4'
            assert scenario['source'] in {'model_ranked', 'deterministic_fallback'}
            assert scenario['suggestions']
            assert scenario['preview'].startswith('🥗 What to eat next')
            candidates = {item['food_id']: item for item in scenario['candidates']}
            assert set(scenario['suggestions']) <= candidates.keys()
            if scenario['scenario'] == 'early_evening':
                assert scenario['timing']['minutes_until_bedtime'] == 300
                assert scenario['candidates'][0]['meal_type'] == 'full_meal'
                assert any(candidates[item]['meal_type'] == 'full_meal' for item in scenario['suggestions'])
            else:
                assert scenario['timing']['minutes_until_bedtime'] == 60
                assert candidates[scenario['suggestions'][0]]['meal_type'] in {'light_meal', 'snack', 'protein_top_up'}
                assert candidates[scenario['suggestions'][0]]['fat_g'] <= 12
            print(f"{scenario['scenario']}: {scenario['source']}; first={scenario['suggestions'][0]}")
        result['unchanged_partition'] = True
        output = Path('artifacts/e2e/recommendation-scenarios.json')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + '\n')
        print(f'Read-only scenarios passed; report: {output}')
    finally:
        reset_e2e_account(table=table, repository=repo)


if __name__ == '__main__':
    main()
