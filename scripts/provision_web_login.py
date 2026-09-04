#!/usr/bin/env python3
"""Provision a known JavaanFitness browser login without storing a password locally."""

from __future__ import annotations

import argparse
import getpass
import os

import boto3

from macro_bot.serverless_auth import hash_web_password, normalize_web_username
from macro_bot.serverless_data import DynamoNutritionRepository, WebCredentialExists


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True, help="Known browser username to provision")
    parser.add_argument("--telegram-user-id", required=True, type=int, help="Existing Telegram-linked user id")
    parser.add_argument("--table-name", default=os.getenv("FITNESS_DATA_TABLE", ""), help="FitnessDataTable name")
    parser.add_argument("--replace", action="store_true", help="Replace the existing password for this username")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        username = normalize_web_username(args.username)
    except ValueError as err:
        _parser().error(str(err))
    if not args.table_name:
        _parser().error("--table-name or FITNESS_DATA_TABLE is required")

    table = boto3.resource("dynamodb").Table(args.table_name)
    repository = DynamoNutritionRepository(table, table_name=args.table_name)
    identity = repository.get_identity(args.telegram_user_id)
    if identity is None:
        _parser().error("the target Telegram-linked JavaanFitness user does not exist")
    existing = repository.get_web_credential(username)
    if existing is not None:
        if not args.replace:
            _parser().error("browser username already exists; pass --replace to reset its password")
        if str(existing.get("user_id", "")) != identity.user_id or int(existing.get("telegram_user_id", 0) or 0) != identity.telegram_user_id:
            _parser().error("existing browser username is mapped to a different JavaanFitness user")

    password = getpass.getpass("Browser password (input hidden): ")
    confirmation = getpass.getpass("Repeat browser password: ")
    if password != confirmation:
        _parser().error("passwords do not match")
    try:
        password_record = hash_web_password(password)
    except ValueError as err:
        _parser().error(str(err))
    try:
        repository.save_web_credential(
            username,
            identity=identity,
            password_record=password_record,
            replace=args.replace,
        )
    except WebCredentialExists as err:
        _parser().error(str(err))
    print("Browser login provisioned for the existing JavaanFitness user.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
