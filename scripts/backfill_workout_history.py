#!/usr/bin/env python3
"""Add workout locators and summaries; dry-run unless --apply is supplied.

Offline administrative discovery may scan. Request-serving code never does.
Run after deploying the writer and before releasing the history frontend.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from boto3.dynamodb.conditions import Attr, Key
from macro_bot.dynamo_store import is_conditional_failure as _is_conditional_failure
from macro_bot.dynamo_workout_repository import DynamoWorkoutRepository


def backfill(table, *, apply=False, user_id=None):
    report = {"sessions": 0, "missing": 0, "written": 0, "existing": 0}
    kwargs = {"ConsistentRead": True}
    if user_id:
        kwargs["KeyConditionExpression"] = Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("WORKOUT#")
        read = table.query
    else:
        kwargs["FilterExpression"] = Attr("entity_type").eq("workout_session")
        read = table.scan
    while True:
        page = read(**kwargs)
        for candidate in page.get("Items", []):
            if candidate.get("entity_type") != "workout_session":
                continue
            # Re-read each authoritative session, since discovery may span pages.
            session = table.get_item(Key={k: candidate[k] for k in ("PK", "SK")}, ConsistentRead=True).get("Item")
            if not session:
                continue
            report["sessions"] += 1
            items = [DynamoWorkoutRepository.locator_item(session)]
            if session.get("status") in {"completed", "cancelled"}:
                items.append(DynamoWorkoutRepository.history_item(session))
            for item in items:
                key = {k: item[k] for k in ("PK", "SK")}
                existing = table.get_item(Key=key, ConsistentRead=True).get("Item")
                if existing:
                    if existing != item:
                        raise RuntimeError("Conflicting derived workout record; no overwrite performed")
                    report["existing"] += 1
                    continue
                report["missing"] += 1
                if apply:
                    try:
                        table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
                        report["written"] += 1
                    except Exception as err:
                        if not _is_conditional_failure(err):
                            raise
                        if table.get_item(Key=key, ConsistentRead=True).get("Item") != item:
                            raise RuntimeError("Concurrent derived record differs; rerun dry-run") from err
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return report


def main():
    import boto3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table-name", required=True)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--user-id", help="Optional internal user ID; queries only that user's workout prefix")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    table = boto3.Session(profile_name=args.profile, region_name=args.region).resource("dynamodb").Table(args.table_name)
    print(json.dumps({"dry_run": not args.apply, **backfill(table, apply=args.apply, user_id=args.user_id)}, sort_keys=True))


if __name__ == "__main__":
    main()
