"""Minimal Telegram webhook ingress for the AWS foundation."""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_secret_cache: Optional[str] = None


def _response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, separators=(",", ":")),
    }


def _header(headers: Mapping[str, Any], name: str) -> str:
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == wanted:
            return str(value or "")
    return ""


def _configured_secret() -> str:
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache

    parameter_name = os.getenv("WEBHOOK_SECRET_PARAMETER_NAME", "").strip()
    if not parameter_name:
        return ""

    try:
        import boto3

        result = boto3.client("ssm").get_parameter(Name=parameter_name, WithDecryption=True)
        _secret_cache = str(result["Parameter"]["Value"])
    except Exception:
        logger.exception("webhook_secret_unavailable")
        # Do not cache a missing/transient lookup so a later secret creation
        # can be observed by a warm execution environment.
        return ""
    return _secret_cache


def _decode_body(event: Mapping[str, Any]) -> Optional[str]:
    body = event.get("body")
    if body is None:
        return None
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(body).decode("utf-8")
        except Exception:
            return None
    return str(body)


def _extract_update_metadata(update: Mapping[str, Any]) -> Tuple[Optional[int], Optional[int], str]:
    update_id = update.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        update_id = None

    for update_type in ("message", "edited_message", "channel_post", "edited_channel_post"):
        message = update.get(update_type)
        if isinstance(message, Mapping):
            chat = message.get("chat")
            chat_id = chat.get("id") if isinstance(chat, Mapping) else None
            return update_id, chat_id if isinstance(chat_id, int) else None, update_type

    callback = update.get("callback_query")
    if isinstance(callback, Mapping):
        message = callback.get("message")
        chat = message.get("chat") if isinstance(message, Mapping) else None
        chat_id = chat.get("id") if isinstance(chat, Mapping) else None
        return update_id, chat_id if isinstance(chat_id, int) else None, "callback_query"

    for update_type in ("inline_query", "chosen_inline_result", "shipping_query", "pre_checkout_query"):
        if update_type in update:
            return update_id, None, update_type
    return update_id, None, "unknown"


def _fallback_group_id(update: Mapping[str, Any], update_id: Optional[int]) -> str:
    if update_id is not None:
        return f"UPDATE#{update_id}"
    return "UPDATE#unknown"


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    headers = event.get("headers") or {}
    expected_secret = _configured_secret()
    received_secret = _header(headers, "X-Telegram-Bot-Api-Secret-Token")
    if not expected_secret or received_secret != expected_secret:
        logger.warning("webhook_rejected reason=invalid_secret")
        return _response(403, {"error": "forbidden"})

    raw_body = _decode_body(event)
    if not raw_body:
        return _response(400, {"error": "invalid_body"})
    try:
        update = json.loads(raw_body)
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid_json"})
    if not isinstance(update, dict):
        return _response(400, {"error": "invalid_update"})

    update_id, chat_id, update_type = _extract_update_metadata(update)
    if update_id is None:
        return _response(400, {"error": "missing_update_id"})

    try:
        import boto3

        queue_url = os.environ["TELEGRAM_QUEUE_URL"]
        group_id = f"CHAT#{chat_id}" if chat_id is not None else _fallback_group_id(update, update_id)
        message = {
            "schema_version": 1,
            "kind": "telegram_update",
            "update_id": update_id,
            "telegram_user_id": _telegram_user_id(update),
            "chat_id": chat_id,
            "update_type": update_type,
            "payload": update,
        }
        boto3.client("sqs").send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(message, separators=(",", ":")),
            MessageGroupId=group_id,
            MessageDeduplicationId=f"telegram-update-{update_id}",
        )
    except Exception:
        logger.exception("webhook_enqueue_failed update_id=%s", update_id)
        return _response(500, {"error": "enqueue_failed"})

    logger.info("webhook_accepted update_id=%s update_type=%s", update_id, update_type)
    return _response(202, {"accepted": True})


def _telegram_user_id(update: Mapping[str, Any]) -> Optional[int]:
    candidates = []
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        candidates.append(update.get(key))
    callback = update.get("callback_query")
    if isinstance(callback, Mapping):
        candidates.append(callback.get("message"))
        candidates.append(callback.get("from"))
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        user = candidate.get("from") if isinstance(candidate.get("from"), Mapping) else candidate
        user_id = user.get("id") if isinstance(user, Mapping) else None
        if isinstance(user_id, int) and not isinstance(user_id, bool):
            return user_id
    return None
