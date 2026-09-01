"""Stateless Telegram SQS worker for the DynamoDB-backed nutrition service."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
from collections.abc import Mapping
from typing import Any, Dict, Optional

from macro_bot.direct_estimator import DirectEstimationError, DirectOpenAIEstimator
from macro_bot.formatting import (
    build_meal_keyboard,
    build_setup_keyboard,
    format_pending_message,
    format_profile_setup_message,
    format_recommendation_message,
    parse_meal_datetime,
)
from macro_bot.serverless_auth import SSMParameterCache, bot_token_from_environment
from macro_bot.serverless_data import (
    ActionExpired,
    ActionFinalized,
    ActionNotFound,
    DynamoNutritionRepository,
)
from macro_bot.serverless_service import InvalidUserInput, NutritionService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_secret_cache = SSMParameterCache()


class NonRetryableUpdate(ValueError):
    """User/input failure that should be acknowledged by SQS."""


def _dynamodb():
    import boto3

    return boto3.client("dynamodb")


def claim_idempotency(
    client: Any,
    table_name: str,
    update_id: int,
    user_id: Any = None,
    source: str = "foundation",
) -> bool:
    now = int(time.time())
    item = {
        "PK": {"S": f"TELEGRAM_UPDATE#{source}#{update_id}"},
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


def complete_idempotency(client: Any, table_name: str, update_id: int, source: str = "foundation") -> None:
    client.update_item(
        TableName=table_name,
        Key={
            "PK": {"S": f"TELEGRAM_UPDATE#{source}#{update_id}"},
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


def release_idempotency(client: Any, table_name: str, update_id: int, source: str = "telegram") -> None:
    """Release a claimed update so a transient failure can be retried."""

    client.delete_item(
        TableName=table_name,
        Key={
            "PK": {"S": f"TELEGRAM_UPDATE#{source}#{update_id}"},
            "SK": {"S": "RECORD"},
        },
        ConditionExpression="#status = :processing",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":processing": {"S": "processing"}},
    )


def _record_from_sqs(record: Mapping[str, Any]) -> Dict[str, Any]:
    body = record.get("body")
    if not isinstance(body, str):
        raise NonRetryableUpdate("SQS record body must be a string")
    try:
        message = json.loads(body)
    except json.JSONDecodeError as err:
        raise NonRetryableUpdate("invalid SQS message JSON") from err
    if not isinstance(message, dict) or message.get("schema_version") != 1:
        raise NonRetryableUpdate("unsupported Telegram message schema")
    if message.get("kind") != "telegram_update":
        raise NonRetryableUpdate("unsupported Telegram message kind")
    if not isinstance(message.get("update_id"), int) or isinstance(message.get("update_id"), bool):
        raise NonRetryableUpdate("missing update_id")
    return message


def _user_from_mapping(user: Any) -> Optional[dict[str, Any]]:
    if not isinstance(user, Mapping):
        return None
    try:
        user_id = int(user.get("id", 0) or 0)
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None
    username = str(user.get("username", "") or "").strip()
    first_name = str(user.get("first_name", "") or "").strip()
    last_name = str(user.get("last_name", "") or "").strip()
    display_name = " ".join(part for part in (first_name, last_name) if part).strip() or username or str(user_id)
    return {"telegram_user_id": user_id, "username": username, "display_name": display_name}


def _message_and_user(update: Mapping[str, Any]) -> tuple[Optional[Mapping[str, Any]], Optional[dict[str, Any]]]:
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        message = update.get(key)
        if isinstance(message, Mapping):
            user = _user_from_mapping(message.get("from"))
            return message, user
    callback = update.get("callback_query")
    if isinstance(callback, Mapping):
        user = _user_from_mapping(callback.get("from"))
        message = callback.get("message")
        return message if isinstance(message, Mapping) else None, user
    return None, None


def _chat_id(message: Mapping[str, Any]) -> Optional[int]:
    chat = message.get("chat")
    if not isinstance(chat, Mapping):
        return None
    try:
        value = int(chat.get("id"))
    except (TypeError, ValueError):
        return None
    return value


def _message_id(message: Mapping[str, Any]) -> int:
    try:
        return int(message.get("message_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _repository() -> DynamoNutritionRepository:
    import boto3

    table_name = os.environ["FITNESS_DATA_TABLE"]
    table = boto3.resource("dynamodb").Table(table_name)
    # Transactions use the low-level client because the resource-bound client
    # applies its own serializer to AttributeValue maps.
    return DynamoNutritionRepository(table, table_name=table_name, client=boto3.client("dynamodb"))


def _service() -> NutritionService:
    return NutritionService(_repository())


def _bot():
    from telegram import Bot

    return Bot(token=bot_token_from_environment(_secret_cache))


def _estimator() -> DirectOpenAIEstimator:
    return DirectOpenAIEstimator(key_cache=_secret_cache)


async def _send(bot: Any, chat_id: int, text: str, reply_markup: Any = None) -> Any:
    return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


async def _handle_recommendation(service: NutritionService, identity: Any, bot: Any, chat_id: int) -> None:
    try:
        result, _prepared = service.recommendation(identity)
    except KeyError:
        url = os.getenv("MINI_APP_URL", "").strip()
        await _send(bot, chat_id, format_profile_setup_message(url), build_setup_keyboard(url))
        return
    await _send(bot, chat_id, format_recommendation_message(result))


async def _handle_photo(
    service: NutritionService,
    identity: Any,
    bot: Any,
    message: Mapping[str, Any],
    update_id: int,
    estimator: Any,
) -> None:
    photos = message.get("photo")
    if not isinstance(photos, list) or not photos:
        raise NonRetryableUpdate("photo update has no photo sizes")
    photo = photos[-1]
    if not isinstance(photo, Mapping) or not photo.get("file_id"):
        raise NonRetryableUpdate("photo update has no file id")
    chat_id = _chat_id(message)
    if chat_id is None:
        raise NonRetryableUpdate("photo update has no chat")
    request_message_id = _message_id(message)
    prior_action = service.find_action_for_update(identity, update_id)
    if prior_action is not None:
        # A retry after the durable create must not estimate or create a second meal.
        service.consume_meal_datetime(identity)
        if prior_action.message_id is None:
            sent = await _send(
                bot,
                chat_id,
                format_pending_message(prior_action),
                build_meal_keyboard(prior_action.token),
            )
            sent_message_id = getattr(sent, "message_id", None)
            if sent_message_id is None and isinstance(sent, Mapping):
                sent_message_id = sent.get("message_id")
            if sent_message_id is not None:
                service.set_action_message_id(identity, prior_action.token, int(sent_message_id))
        return
    telegram_file = await bot.get_file(photo["file_id"])
    output = io.BytesIO()
    await telegram_file.download_to_memory(out=output)
    image_bytes = output.getvalue()
    caption = str(message.get("caption", "") or "")[:1000]
    persona_hint = service.persona_hint(identity, caption)
    try:
        estimation = await estimator.estimate(image_bytes, caption=caption, persona_hint=persona_hint)
    except DirectEstimationError as err:
        if getattr(err, "retryable", True):
            raise
        await _send(bot, chat_id, "❌ That image could not be estimated. Please resend a clearer meal photo.")
        return
    selected_datetime = service.peek_meal_datetime(identity)
    eaten_at = None
    if selected_datetime:
        from macro_bot.serverless_data import parse_utc

        eaten_at = parse_utc(selected_datetime)
    else:
        eaten_at = service.current_local_now_utc(identity)
    action = service.create_pending_meal(
        identity,
        chat_id=chat_id,
        request_message_id=request_message_id,
        caption=caption,
        estimate=estimation.estimate,
        eaten_at=eaten_at,
        username=identity.username,
        update_id=update_id,
        model_metadata={"model": estimation.model, "usage": estimation.usage},
    )
    # Clear the selected time only after the meal/action transaction succeeds.
    service.consume_meal_datetime(identity)
    sent = await _send(bot, chat_id, format_pending_message(action), build_meal_keyboard(action.token))
    sent_message_id = getattr(sent, "message_id", None)
    if sent_message_id is None and isinstance(sent, Mapping):
        sent_message_id = sent.get("message_id")
    if sent_message_id is not None:
        service.set_action_message_id(identity, action.token, int(sent_message_id))


async def _handle_callback(service: NutritionService, identity: Any, bot: Any, update: Mapping[str, Any]) -> None:
    callback = update.get("callback_query")
    if not isinstance(callback, Mapping):
        raise NonRetryableUpdate("callback update is malformed")
    callback_id = str(callback.get("id", "") or "")
    data = str(callback.get("data", "") or "")
    parts = data.split(":")
    if len(parts) != 4 or parts[:2] != ["meal", "v1"]:
        if callback_id:
            await bot.answer_callback_query(callback_query_id=callback_id)
        return
    action_name, token = parts[2], parts[3]
    if action_name not in {"smaller", "larger", "confirm", "cancel"} or not token:
        if callback_id:
            await bot.answer_callback_query(callback_query_id=callback_id)
        return
    action = service.get_action(identity, token)
    if action is None:
        if callback_id:
            await bot.answer_callback_query(callback_query_id=callback_id, text="Not your meal or meal expired.")
        return
    try:
        if action_name in {"smaller", "larger"}:
            action = service.scale_action(identity, token, 0.8 if action_name == "smaller" else 1.2)
            if callback_id:
                await bot.answer_callback_query(callback_query_id=callback_id)
            message = callback.get("message")
            if isinstance(message, Mapping):
                chat_id = _chat_id(message)
                message_id = _message_id(message)
                if chat_id is not None and message_id:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=format_pending_message(action),
                        reply_markup=build_meal_keyboard(token),
                    )
            return
        result = service.finalize_action(identity, token, "confirm" if action_name == "confirm" else "cancel")
    except (ActionNotFound, ActionExpired, ActionFinalized) as err:
        if callback_id:
            await bot.answer_callback_query(callback_query_id=callback_id, text=str(err)[:180])
        return
    if callback_id:
        await bot.answer_callback_query(callback_query_id=callback_id)
    message = callback.get("message")
    if isinstance(message, Mapping):
        chat_id = _chat_id(message)
        message_id = _message_id(message)
        if chat_id is not None and message_id:
            if result.status == "confirmed":
                text = f"{format_pending_message(result.action)}\n\n✅ Logged"
            else:
                text = f"{format_pending_message(result.action)}\n\n❌ Cancelled — please re-upload the photo (add caption if possible)."
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=None)
            if result.status == "confirmed" and not result.duplicate:
                await _handle_recommendation(service, identity, bot, chat_id)


async def process_update_message(message: Mapping[str, Any], *, service: Any = None, bot: Any = None, estimator: Any = None) -> None:
    update = message.get("payload") if isinstance(message, Mapping) else None
    if not isinstance(update, Mapping):
        raise NonRetryableUpdate("Telegram update payload is malformed")
    if not update:
        return
    message_payload, user = _message_and_user(update)
    if user is None:
        return
    service = service or _service()
    bot = bot or _bot()
    estimator = estimator or _estimator()
    identity = service.resolve_user(
        user["telegram_user_id"],
        username=user["username"],
        display_name=user["display_name"],
    )
    update_id = int(message["update_id"])
    if isinstance(update.get("callback_query"), Mapping):
        await _handle_callback(service, identity, bot, update)
        return
    if not isinstance(message_payload, Mapping):
        return
    chat_id = _chat_id(message_payload)
    if chat_id is None:
        return
    text = str(message_payload.get("text", "") or "").strip()
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
    if command == "/logmeal":
        service.begin_logmeal(identity)
        await _send(bot, chat_id, "Send meal date/time in this format: DD-MM-YYYY HH:MM")
        return
    if command == "/openapp":
        url = os.getenv("MINI_APP_URL", "").strip()
        await _send(bot, chat_id, format_profile_setup_message(url), build_setup_keyboard(url))
        return
    if command == "/suggestmeal":
        await _handle_recommendation(service, identity, bot, chat_id)
        return
    if message_payload.get("photo"):
        await _handle_photo(service, identity, bot, message_payload, update_id, estimator)
        return
    workflow = service.repository.get_workflow(identity.user_id)
    if workflow and workflow.get("state") == "awaiting_datetime" and text:
        try:
            parsed = parse_meal_datetime(text, "%d-%m-%Y %H:%M")
            normalized = service.normalize_user_datetime(identity, parsed)
        except (TypeError, ValueError) as err:
            raise NonRetryableUpdate(str(err)) from err
        if not service.set_meal_datetime(identity, normalized):
            await _send(bot, chat_id, "That meal logging prompt has expired. Send /logmeal to start again.")
            return
        await _send(bot, chat_id, f"✅ Meal time set: {text}. Now send the meal photo.")


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    client = _dynamodb()
    table_name = os.environ["IDEMPOTENCY_TABLE"]
    failures = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId", ""))
        update_id: Optional[int] = None
        claimed = False
        try:
            message = _record_from_sqs(record)
            update_id = int(message["update_id"])
            payload = message.get("payload")
            if isinstance(payload, Mapping) and payload.get("foundation_test_behavior") == "fail":
                raise RuntimeError("intentional foundation retry test failure")
            claimed = claim_idempotency(
                client,
                table_name,
                update_id,
                message.get("telegram_user_id"),
                source="telegram",
            )
            if not claimed:
                logger.info("worker_duplicate update_id=%s", update_id)
                continue
            asyncio.run(process_update_message(message))
            complete_idempotency(client, table_name, update_id, source="telegram")
            logger.info("worker_acknowledged update_id=%s", update_id)
        except NonRetryableUpdate:
            if claimed and update_id is not None:
                complete_idempotency(client, table_name, update_id, source="telegram")
            logger.info("worker_input_rejected message_id=%s", message_id)
        except Exception as err:
            # External SDK exceptions can contain request URLs or payload
            # fragments; keep operational logs metadata-only.
            logger.error("worker_record_failed message_id=%s error_type=%s", message_id, type(err).__name__)
            if claimed and update_id is not None:
                try:
                    release_idempotency(client, table_name, update_id, source="telegram")
                except Exception as err:
                    logger.error("worker_idempotency_release_failed update_id=%s error_type=%s", update_id, type(err).__name__)
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
