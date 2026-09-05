#!/usr/bin/env python3
"""Reset only the marked JavaanFitness dev E2E user's mutable data."""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from macro_bot.models import MealEstimate
from macro_bot.serverless_data import DynamoNutritionRepository
from macro_bot.serverless_service import NutritionService
from scripts.e2e_support import (
    BASELINE_PROFILE_PAYLOAD,
    E2E_USERNAME,
    E2E_USER_ID,
    E2EAccountSafetyError,
    aws_session,
    dev_resources,
    identity_from_record,
    read_e2e_records,
    user_partition_items,
    validate_e2e_credential,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes",
        action="store_true",
        help=f"confirm reset of the marked {E2E_USERNAME} account",
    )
    return parser


def _confirm() -> bool:
    try:
        answer = input(f"Type {E2E_USERNAME} to reset only that E2E account: ").strip()
    except EOFError:
        return False
    return answer == E2E_USERNAME


def delete_user_partition(table: Any, user_id: str) -> int:
    """Delete records from exactly one already-validated user partition."""

    items = user_partition_items(table, user_id)
    if not items:
        return 0
    with table.batch_writer() as batch:
        for item in items:
            if item.get("PK") != f"USER#{user_id}":
                raise E2EAccountSafetyError("refusing to delete an unexpected partition")
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    return len(items)


def reset_e2e_account(*, table: Any, repository: DynamoNutritionRepository) -> int:
    _marker, identity_record = read_e2e_records(table)
    identity = identity_from_record(identity_record)
    credential = repository.get_web_credential(E2E_USERNAME)
    validate_e2e_credential(credential)
    if identity.user_id != E2E_USER_ID or identity.identity_pk != identity_record["PK"]:
        raise E2EAccountSafetyError("the E2E identity does not match the configured account")

    deleted = delete_user_partition(table, E2E_USER_ID)
    service = NutritionService(repository)
    service.save_profile(identity, dict(BASELINE_PROFILE_PAYLOAD))
    # Invalidates queued/in-flight Lab jobs without touching their ephemeral table.
    table.update_item(Key={"PK": f"USER#{E2E_USER_ID}", "SK": "PROFILE"},
                      UpdateExpression="SET e2e_reset_revision = :revision",
                      ExpressionAttributeValues={":revision": uuid.uuid4().hex})
    timezone_name = BASELINE_PROFILE_PAYLOAD["timezone"]
    timezone_info = ZoneInfo(timezone_name)
    local_now = repository._now().astimezone(timezone_info)
    local_day_start = datetime.combine(local_now.date(), time.min, tzinfo=timezone_info)
    eaten_at = max(local_day_start, local_now - timedelta(minutes=5)).astimezone(timezone.utc)
    action = repository.create_pending_meal(
        identity,
        chat_id=0,
        request_message_id=0,
        caption="E2E baseline meal",
        estimate=MealEstimate(
            meal_name="E2E baseline meal",
            calories=600,
            protein_g=40,
            carbs_g=60,
            fat_g=15,
            confidence=1.0,
            notes="Deterministic E2E fixture",
        ),
        eaten_at=eaten_at,
        username=E2E_USERNAME,
    )
    repository.finalize_action(identity, action.token, "confirm")
    return deleted


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.yes and not _confirm():
        print("E2E reset cancelled.")
        return 1
    try:
        session, table, _outputs, repository = dev_resources(aws_session())
        deleted = reset_e2e_account(table=table, repository=repository)
    except E2EAccountSafetyError as err:
        print(f"E2E reset refused: {err}", file=sys.stderr)
        return 2
    print(f"E2E account reset to baseline; deleted {deleted} user records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
