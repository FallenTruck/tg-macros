#!/usr/bin/env python3
"""Create or rotate the isolated JavaanFitness dev E2E browser account."""

from __future__ import annotations

import argparse
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from macro_bot.serverless_auth import hash_web_password, verify_web_password
from macro_bot.serverless_data import DynamoNutritionRepository
from scripts.e2e_support import (
    E2E_IDENTITY_PK,
    E2E_MARKER_PK,
    E2E_MARKER_SK,
    E2E_PASSWORD_PARAMETER,
    E2E_USERNAME,
    E2E_USERNAME_PARAMETER,
    E2E_USER_ID,
    E2EAccountSafetyError,
    dev_resources,
    get_parameter,
    identity_from_record,
    identity_item,
    marker_item,
    user_partition_items,
    validate_e2e_credential,
    aws_session,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="rotate the generated password and replace the existing browser hash",
    )
    return parser


def generate_e2e_password() -> str:
    """Generate the password that stays in memory until SSM accepts it."""

    return secrets.token_urlsafe(32)


def _get_item(table: Any, key: Mapping[str, str]) -> Optional[dict[str, Any]]:
    result = table.get_item(Key=dict(key), ConsistentRead=True)
    item = result.get("Item") if isinstance(result, Mapping) else None
    return dict(item) if item else None


def ensure_e2e_identity(table: Any, repository: DynamoNutritionRepository):
    """Create the marker and synthetic identity atomically, or validate both."""

    marker = _get_item(table, {"PK": E2E_MARKER_PK, "SK": E2E_MARKER_SK})
    identity = _get_item(table, {"PK": E2E_IDENTITY_PK, "SK": "USER"})
    if marker is not None or identity is not None:
        if marker is None or identity is None:
            raise E2EAccountSafetyError("the E2E marker and identity are only partially present")
        from scripts.e2e_support import validate_e2e_records

        validate_e2e_records(marker, identity)
        return identity_from_record(identity)

    if user_partition_items(table, E2E_USER_ID):
        raise E2EAccountSafetyError("the reserved E2E user partition is already occupied")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    identity_record = {**identity_item(), "created_at": now, "updated_at": now}
    marker_record = {**marker_item(), "created_at": now, "updated_at": now}
    repository._transact_write(
        [
            {
                "operation": "Put",
                "TableName": repository.table_name,
                "Item": identity_record,
                "ConditionExpression": "attribute_not_exists(PK)",
            },
            {
                "operation": "Put",
                "TableName": repository.table_name,
                "Item": marker_record,
                "ConditionExpression": "attribute_not_exists(PK)",
            },
        ]
    )
    return identity_from_record(identity_record)


def _ensure_username_parameter(ssm: Any) -> None:
    current = get_parameter(ssm, E2E_USERNAME_PARAMETER)
    if current is not None and current != E2E_USERNAME:
        raise E2EAccountSafetyError("the E2E username parameter contains an unexpected value")
    if current is None:
        ssm.put_parameter(
            Name=E2E_USERNAME_PARAMETER,
            Value=E2E_USERNAME,
            Type="String",
            Overwrite=False,
        )


def provision_e2e_account(*, session: Any, table: Any, repository: DynamoNutritionRepository, replace: bool) -> None:
    identity = ensure_e2e_identity(table, repository)
    credential = repository.get_web_credential(E2E_USERNAME)
    if credential is not None:
        validate_e2e_credential(credential)

    ssm = session.client("ssm")
    _ensure_username_parameter(ssm)
    stored_password = get_parameter(ssm, E2E_PASSWORD_PARAMETER)
    if credential is not None and stored_password is not None and not replace:
        if verify_web_password(stored_password, credential):
            print("E2E browser account is already provisioned: javaan-e2e")
            return
        raise E2EAccountSafetyError("the E2E SSM password does not match its DynamoDB hash; pass --replace")
    if credential is not None and stored_password is None and not replace:
        raise E2EAccountSafetyError("the E2E browser credential exists without its SSM password; pass --replace")

    password = generate_e2e_password() if replace or stored_password is None else stored_password
    password_record = hash_web_password(password)
    if stored_password is None or replace:
        ssm.put_parameter(
            Name=E2E_PASSWORD_PARAMETER,
            Value=password,
            Type="SecureString",
            Overwrite=bool(replace),
        )
    repository.save_web_credential(
        E2E_USERNAME,
        identity=identity,
        password_record=password_record,
        replace=bool(credential is not None),
    )
    print("E2E browser account provisioned: javaan-e2e")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        session, table, _outputs, repository = dev_resources(aws_session())
        provision_e2e_account(session=session, table=table, repository=repository, replace=args.replace)
    except E2EAccountSafetyError as err:
        print(f"E2E provisioning refused: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
