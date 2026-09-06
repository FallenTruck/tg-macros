#!/usr/bin/env python3
"""Safely reconcile the deterministic JavaanFitness shared programme seed."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from macro_bot.serverless_data import DynamoNutritionRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table-name", default=os.getenv("FITNESS_DATA_TABLE", "tg-macros-dev-fitness-data"))
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "ap-southeast-1"))
    parser.add_argument("--profile", default=os.getenv("AWS_PROFILE"))
    parser.add_argument("--dry-run", action="store_true", help="reconcile without writing records")
    parser.add_argument("--core-options", action="store_true", help="publish the core-options version after the initial seed")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    import boto3

    session_kwargs: dict[str, Any] = {"region_name": args.region}
    if args.profile:
        session_kwargs["profile_name"] = args.profile
    session = boto3.Session(**session_kwargs)
    table = session.resource("dynamodb").Table(args.table_name)
    repository = DynamoNutritionRepository(
        table,
        table_name=args.table_name,
        client=session.client("dynamodb"),
    )
    report = (repository.publish_core_options_programme(dry_run=args.dry_run) if args.core_options
              else repository.seed_workout_programme(dry_run=args.dry_run))
    print(json.dumps({"table": args.table_name, "dry_run": args.dry_run, **report}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
