"""Shared safety and AWS helpers for the JavaanFitness dev E2E account."""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from boto3.dynamodb.conditions import Key

from macro_bot.serverless_data import DynamoNutritionRepository, ServerlessIdentity

DEV_PROFILE = "fitness-dev"
DEV_REGION = "ap-southeast-1"
DEV_STACK_NAME = "tg-macros-dev"
E2E_USERNAME = "javaan-e2e"
E2E_USER_ID = "e2e-javaan-e2e"
E2E_IDENTITY_PK = f"IDENTITY#E2E#{E2E_USERNAME}"
E2E_MARKER_PK = f"E2E_ACCOUNT#{E2E_USERNAME}"
E2E_MARKER_SK = "META"
E2E_IDENTITY_SK = "USER"
E2E_SYNTHETIC_TELEGRAM_USER_ID = 0
E2E_DISPLAY_NAME = "JavaanFitness E2E"
E2E_USERNAME_PARAMETER = "/tg-macros/dev/e2e/web_username"
E2E_PASSWORD_PARAMETER = "/tg-macros/dev/e2e/web_password"
BASELINE_PROFILE_PAYLOAD = {
    "sex": "male",
    "age_years": 30,
    "height_cm": 180,
    "weight_kg": 80,
    "activity_level": "moderate",
    "goal": "maintain",
    "timezone": "Asia/Singapore",
}


class E2EAccountSafetyError(RuntimeError):
    """Raised when an E2E operation cannot prove it targets the test account."""


def aws_session():
    import boto3

    profile = os.getenv("AWS_PROFILE", DEV_PROFILE)
    region = os.getenv("AWS_REGION", DEV_REGION)
    if profile != DEV_PROFILE:
        raise E2EAccountSafetyError(f"E2E tooling requires AWS profile {DEV_PROFILE}")
    if region != DEV_REGION:
        raise E2EAccountSafetyError(f"E2E tooling requires AWS region {DEV_REGION}")
    return boto3.Session(profile_name=DEV_PROFILE, region_name=DEV_REGION)


def dev_stack_outputs(session: Any) -> dict[str, str]:
    response = session.client("cloudformation").describe_stacks(StackName=DEV_STACK_NAME)
    stacks = response.get("Stacks", []) if isinstance(response, Mapping) else []
    if len(stacks) != 1:
        raise E2EAccountSafetyError(f"documented dev stack {DEV_STACK_NAME} was not found")
    outputs = {
        str(item.get("OutputKey")): str(item.get("OutputValue", ""))
        for item in stacks[0].get("Outputs", [])
        if item.get("OutputKey")
    }
    table_name = outputs.get("FitnessDataTableName", "")
    mini_app_url = outputs.get("MiniAppUrl", "")
    if not table_name or not mini_app_url:
        raise E2EAccountSafetyError("dev stack outputs are missing FitnessDataTableName or MiniAppUrl")
    return outputs


def dev_resources(session: Any = None) -> tuple[Any, Any, dict[str, str], DynamoNutritionRepository]:
    session = session or aws_session()
    outputs = dev_stack_outputs(session)
    table_name = outputs["FitnessDataTableName"]
    table = session.resource("dynamodb").Table(table_name)
    repository = DynamoNutritionRepository(
        table,
        table_name=table_name,
        client=session.client("dynamodb"),
    )
    return session, table, outputs, repository


def get_parameter(ssm: Any, name: str) -> Optional[str]:
    try:
        result = ssm.get_parameter(Name=name, WithDecryption=True)
    except Exception as err:
        error = getattr(err, "response", {})
        code = error.get("Error", {}).get("Code") if isinstance(error, Mapping) else None
        if code == "ParameterNotFound":
            return None
        raise
    parameter = result.get("Parameter", {}) if isinstance(result, Mapping) else {}
    value = parameter.get("Value")
    return str(value) if value is not None else None


def load_e2e_credentials(session: Any) -> tuple[str, str]:
    """Load the E2E credential pair into memory without logging or persisting it."""

    ssm = session.client("ssm")
    username = get_parameter(ssm, E2E_USERNAME_PARAMETER)
    password = get_parameter(ssm, E2E_PASSWORD_PARAMETER)
    if username != E2E_USERNAME or not password:
        raise E2EAccountSafetyError("the E2E SSM credential parameters are not provisioned")
    return username, password


def marker_item() -> dict[str, Any]:
    return {
        "PK": E2E_MARKER_PK,
        "SK": E2E_MARKER_SK,
        "entity_type": "e2e_account_marker",
        "account_type": "e2e",
        "username": E2E_USERNAME,
        "user_id": E2E_USER_ID,
        "identity_pk": E2E_IDENTITY_PK,
        "telegram_user_id": E2E_SYNTHETIC_TELEGRAM_USER_ID,
    }


def identity_item() -> dict[str, Any]:
    return {
        "PK": E2E_IDENTITY_PK,
        "SK": E2E_IDENTITY_SK,
        "entity_type": "identity",
        "account_type": "e2e",
        "telegram_user_id": E2E_SYNTHETIC_TELEGRAM_USER_ID,
        "user_id": E2E_USER_ID,
        "username": E2E_USERNAME,
        "display_name": E2E_DISPLAY_NAME,
    }


def validate_e2e_records(marker: Any, identity: Any) -> None:
    """Prove that two records are the dedicated synthetic account."""

    expected_marker = marker_item()
    expected_identity = identity_item()
    if not isinstance(marker, Mapping) or not isinstance(identity, Mapping):
        raise E2EAccountSafetyError("the E2E marker and identity must both exist")
    for field, expected in expected_marker.items():
        if marker.get(field) != expected:
            raise E2EAccountSafetyError(f"E2E marker failed validation for {field}")
    for field, expected in expected_identity.items():
        if identity.get(field) != expected:
            raise E2EAccountSafetyError(f"E2E identity failed validation for {field}")


def read_e2e_records(table: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    marker_result = table.get_item(Key={"PK": E2E_MARKER_PK, "SK": E2E_MARKER_SK}, ConsistentRead=True)
    identity_result = table.get_item(Key={"PK": E2E_IDENTITY_PK, "SK": E2E_IDENTITY_SK}, ConsistentRead=True)
    marker = marker_result.get("Item") if isinstance(marker_result, Mapping) else None
    identity = identity_result.get("Item") if isinstance(identity_result, Mapping) else None
    validate_e2e_records(marker, identity)
    return dict(marker), dict(identity)


def identity_from_record(identity: Mapping[str, Any]) -> ServerlessIdentity:
    validate_e2e_records(marker_item(), identity)
    return ServerlessIdentity(
        telegram_user_id=E2E_SYNTHETIC_TELEGRAM_USER_ID,
        user_id=E2E_USER_ID,
        username=E2E_USERNAME,
        display_name=E2E_DISPLAY_NAME,
        created_at=str(identity.get("created_at", "")),
        updated_at=str(identity.get("updated_at", identity.get("created_at", ""))),
        identity_key=E2E_IDENTITY_PK,
    )


def validate_e2e_credential(credential: Any) -> None:
    if not isinstance(credential, Mapping):
        raise E2EAccountSafetyError("the E2E browser credential is missing")
    expected = {
        "entity_type": "web_credential",
        "username": E2E_USERNAME,
        "user_id": E2E_USER_ID,
        "telegram_user_id": E2E_SYNTHETIC_TELEGRAM_USER_ID,
        "identity_pk": E2E_IDENTITY_PK,
    }
    for field, value in expected.items():
        if credential.get(field) != value:
            raise E2EAccountSafetyError(f"E2E credential failed validation for {field}")


def user_partition_items(table: Any, user_id: str) -> list[dict[str, Any]]:
    """Read only the exact user partition targeted by a reset."""

    expected_pk = f"USER#{user_id}"
    query_kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(expected_pk),
        "ConsistentRead": True,
    }
    items: list[dict[str, Any]] = []
    while True:
        response = table.query(**query_kwargs)
        items.extend(dict(item) for item in response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        query_kwargs["ExclusiveStartKey"] = last_key
    if any(item.get("PK") != expected_pk for item in items):
        raise E2EAccountSafetyError("the user-partition query returned an unexpected partition")
    return items
