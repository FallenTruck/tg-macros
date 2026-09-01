"""Authenticated Mini App API adapter for the DynamoDB nutrition service."""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from mangum import Mangum

from macro_bot.serverless_auth import (
    SSMParameterCache,
    TelegramAuthError,
    bot_token_from_environment,
    validate_init_data,
)
from macro_bot.serverless_data import DynamoNutritionRepository
from macro_bot.serverless_service import InvalidUserInput, NutritionService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_secret_cache = SSMParameterCache()


def _service() -> NutritionService:
    import boto3

    table_name = os.environ["FITNESS_DATA_TABLE"]
    table = boto3.resource("dynamodb").Table(table_name)
    # Transactions use the low-level client because the resource-bound client
    # applies its own serializer to AttributeValue maps.
    return NutritionService(
        DynamoNutritionRepository(
            table,
            table_name=table_name,
            client=boto3.client("dynamodb"),
        )
    )


def _auth_identity(init_data: str, service: Optional[NutritionService] = None) -> Any:
    try:
        user = validate_init_data(
            init_data,
            bot_token_from_environment(_secret_cache),
            max_age_seconds=int(os.getenv("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "3600")),
            future_skew_seconds=int(os.getenv("TELEGRAM_INIT_DATA_FUTURE_SKEW_SECONDS", "60")),
        )
    except (TelegramAuthError, ValueError, RuntimeError) as err:
        logger.info("miniapp_auth_rejected reason=%s", type(err).__name__)
        raise HTTPException(status_code=401, detail=str(err)) from err
    return (service or _service()).resolve_user(user.telegram_user_id, user.username, user.display_name)


def _no_store(content: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=content, status_code=status_code, headers={"cache-control": "no-store"})


@app.get("/api")
@app.get("/api/health")
async def health(request: Request) -> JSONResponse:
    return _no_store(
        {
            "ok": True,
            "service": "tg-macros-api",
            "path": request.url.path,
            "telegram_init_data_present": bool(request.headers.get("x-telegram-init-data")),
        }
    )


@app.get("/api/profile")
async def get_profile(
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(x_telegram_init_data, service)
    return _no_store(service.profile_response(identity))


@app.post("/api/targets/preview")
async def preview_targets(
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    _auth_identity(x_telegram_init_data)
    try:
        result = NutritionService.preview_payload(payload)
    except InvalidUserInput as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return _no_store(result)


@app.post("/api/profile")
async def save_profile(
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(x_telegram_init_data, service)
    try:
        result = service.save_profile(identity, payload)
    except InvalidUserInput as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return _no_store(result)


@app.get("/api/targets")
async def get_targets(
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(x_telegram_init_data, service)
    return _no_store({"targets": service.target_history(identity)})


@app.post("/api/targets")
async def save_target(
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    """Append a target revision; questionnaire data remains the source of calculation."""

    service = _service()
    identity = _auth_identity(x_telegram_init_data, service)
    profile_payload = dict(payload)
    answers = profile_payload.get("questionnaire_answers")
    if isinstance(answers, dict):
        profile_payload.update(answers)
    try:
        result = service.save_profile(identity, profile_payload)
    except InvalidUserInput as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return _no_store(result)


@app.get("/api/meals")
async def get_meals(
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
    date_start: Optional[date] = Query(default=None),
    date_end: Optional[date] = Query(default=None),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(x_telegram_init_data, service)
    if date_start is not None and date_end is not None and date_end < date_start:
        raise HTTPException(status_code=400, detail="date_end must not precede date_start")
    return _no_store(service.meals_payload(identity, date_start, date_end or date_start))


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def unknown_api_route(path: str) -> JSONResponse:
    return _no_store({"error": "not_found", "path": f"/api/{path}"}, status_code=404)


handler = Mangum(app, lifespan="off")
