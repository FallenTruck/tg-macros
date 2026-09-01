"""Foundation SQS worker: validation, idempotency and acknowledgement only."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Mapping

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _dynamodb():
    import boto3

    return boto3.client("dynamodb")


def claim_idempotency(client: Any, table_name: str, update_id: int, user_id: Any = None) -> bool:
    now = int(time.time())
    item = {
        "PK": {"S": f"TELEGRAM_UPDATE#foundation#{update_id}"},
        "SK": {"S": "RECORD"},
        "status": {"S": "processing"},
        "started_at": {"N": str(now)},
        "expires_at": {"N": str(now + 86400)},
    }
    if user_id is not None:
        item["telegram_user_id"] = {"N": str(user_id)}
    try:
        client.put_item(
            TableName=table_name,
            Item=item,
            ConditionExpression="attribute_not_exists(PK)",
        )
        return True
    except client.exceptions.ConditionalCheckFailedException:
        return False


def complete_idempotency(client: Any, table_name: str, update_id: int) -> None:
    client.update_item(
        TableName=table_name,
        Key={
            "PK": {"S": f"TELEGRAM_UPDATE#foundation#{update_id}"},
            "SK": {"S": "RECORD"},
        },
        UpdateExpression="SET #status = :completed, completed_at = :completed_at",
        ConditionExpression="#status = :processing",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":processing": {"S": "processing"},
            ":completed": {"S": "completed"},
            ":completed_at": {"N": str(int(time.time()))},
        },
    )


def _record_from_sqs(record: Mapping[str, Any]) -> Dict[str, Any]:
    body = record.get("body")
    if not isinstance(body, str):
        raise ValueError("SQS record body must be a string")
    message = json.loads(body)
    if not isinstance(message, dict) or message.get("schema_version") != 1:
        raise ValueError("unsupported foundation message")
    if message.get("kind") != "telegram_update":
        raise ValueError("unsupported foundation message kind")
    if not isinstance(message.get("update_id"), int):
        raise ValueError("missing update_id")
    return message


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    client = _dynamodb()
    table_name = os.environ["IDEMPOTENCY_TABLE"]
    failures = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId", ""))
        try:
            message = _record_from_sqs(record)
            payload = message.get("payload")
            if isinstance(payload, Mapping) and payload.get("foundation_test_behavior") == "fail":
                raise RuntimeError("intentional foundation retry test failure")

            claimed = claim_idempotency(
                client,
                table_name,
                message["update_id"],
                message.get("telegram_user_id"),
            )
            if not claimed:
                logger.info("worker_duplicate update_id=%s", message["update_id"])
                continue
            complete_idempotency(client, table_name, message["update_id"])
            logger.info("worker_acknowledged update_id=%s", message["update_id"])
        except Exception:
            logger.exception("worker_record_failed message_id=%s", message_id)
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
