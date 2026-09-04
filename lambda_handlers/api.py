"""Authenticated Mini App API adapter for the DynamoDB nutrition service."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import date
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from mangum import Mangum

from macro_bot.serverless_auth import (
    SSMParameterCache,
    TelegramAuthError,
    normalize_web_username,
    bot_token_from_environment,
    validate_init_data,
    verify_web_password,
)
from macro_bot.serverless_data import BROWSER_SESSION_TTL_SECONDS, DynamoNutritionRepository
from macro_bot.serverless_service import InvalidUserInput, NutritionService
from macro_bot.workout_execution import InvalidWorkoutInput, WorkoutConflict, WorkoutNotFound

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_secret_cache = SSMParameterCache()
BROWSER_SESSION_COOKIE = "jf_session"
GENERIC_LOGIN_ERROR = "Invalid username or password."
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _user_fingerprint(user_id: Any) -> str:
    if user_id is None or user_id == "":
        return "none"
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:12]


def _viewer_payload(identity: Any) -> dict[str, Any]:
    return {
        "telegram_user_id": int(getattr(identity, "telegram_user_id", 0) or 0),
        "username": str(getattr(identity, "username", "") or ""),
        "display_name": str(getattr(identity, "display_name", "") or ""),
    }


def _log_workout_api_event(
    event: str,
    identity: Any = None,
    *,
    stage: str = "",
    operation: str = "",
    result: str = "success",
    error_category: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Log workout API lifecycle events without private workout values."""

    logger.info(
        "%s user_fingerprint=%s stage=%s operation=%s duration_ms=%s result=%s error_category=%s",
        event,
        _user_fingerprint(getattr(identity, "user_id", None)),
        stage or "none",
        operation or "none",
        "none" if duration_ms is None else max(0, int(duration_ms)),
        result,
        error_category or "none",
    )


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


def _browser_session_identity(request: Request, service: NutritionService) -> Any:
    repository = getattr(service, "repository", None)
    if repository is None or not hasattr(repository, "get_browser_session") or not hasattr(repository, "get_identity"):
        raise HTTPException(status_code=401, detail="Browser session is missing or expired.")
    session = repository.get_browser_session(request.cookies.get(BROWSER_SESSION_COOKIE, ""))
    if session is None:
        raise HTTPException(status_code=401, detail="Browser session is missing or expired.")
    try:
        telegram_user_id = int(session.get("telegram_user_id", 0) or 0)
        user_id = str(session.get("user_id", "") or "")
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Browser session is invalid.")
    identity = repository.get_identity(telegram_user_id)
    if identity is None or not user_id or identity.user_id != user_id:
        logger.info("browser_session_rejected reason=identity_mismatch")
        raise HTTPException(status_code=401, detail="Browser session is invalid.")
    _log_workout_api_event("browser_auth_success", identity, stage="auth")
    return identity


def _auth_context(request_or_init_data: Any, service: Optional[NutritionService] = None) -> tuple[Any, Optional[dict[str, Any]]]:
    request = request_or_init_data if isinstance(request_or_init_data, Request) else None
    init_data = request.headers.get("x-telegram-init-data", "") if request is not None else str(request_or_init_data or "")
    if not str(init_data).strip():
        if request is None:
            raise HTTPException(status_code=401, detail="Missing X-Telegram-Init-Data header")
        return _browser_session_identity(request, service or _service()), None
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
    service = service or _service()
    launch = None
    if user.start_param:
        repository = getattr(service, "repository", None)
        if repository is None or not hasattr(repository, "get_mini_app_launch"):
            raise HTTPException(status_code=401, detail="Mini App launch context is unavailable")
        launch = repository.get_mini_app_launch(user.start_param)
        if launch is None:
            raise HTTPException(status_code=401, detail="Mini App launch token is invalid or expired")
        if int(launch.get("telegram_user_id", 0) or 0) != user.telegram_user_id:
            raise HTTPException(status_code=403, detail="Mini App launch user mismatch")
        launch_chat_type = str(launch.get("chat_type", "") or "").strip().lower()
        if launch_chat_type and user.chat_type and launch_chat_type != user.chat_type:
            raise HTTPException(status_code=403, detail="Mini App launch chat mismatch")
    identity = service.resolve_user(user.telegram_user_id, user.username, user.display_name)
    if launch is not None and str(launch.get("user_id", "")) != str(identity.user_id):
        raise HTTPException(status_code=403, detail="Mini App launch identity mismatch")
    if launch is not None:
        _log_workout_api_event(
            "miniapp_launch_context_resolved",
            identity,
            stage="launch_context",
            operation=str(launch.get("launch_type", "nutrition")),
        )
    _log_workout_api_event("miniapp_auth_success", identity, stage="auth")
    logger.info(
        "miniapp_auth_succeeded start_param_present=%s chat_type=%s chat_instance_present=%s",
        bool(user.start_param),
        user.chat_type or "none",
        bool(user.chat_instance),
    )
    return identity, launch


def _auth_identity(request_or_init_data: Any, service: Optional[NutritionService] = None) -> Any:
    identity, _launch = _auth_context(request_or_init_data, service)
    return identity


def _no_store(content: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=content, status_code=status_code, headers={"cache-control": "no-store"})


def _configured_browser_origin() -> str:
    configured = urlsplit(os.getenv("MINI_APP_URL", "").strip())
    if configured.scheme not in {"http", "https"} or not configured.netloc:
        return ""
    return f"{configured.scheme.lower()}://{configured.netloc.lower()}"


def _request_origin(request: Request) -> str:
    origin = request.headers.get("origin", "").strip()
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme and parsed.netloc and not parsed.path and not parsed.query and not parsed.fragment:
            return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        return ""
    referer = request.headers.get("referer", "").strip()
    if not referer:
        return ""
    parsed = urlsplit(referer)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _is_same_origin_request(request: Request) -> bool:
    expected = _configured_browser_origin()
    return bool(expected and _request_origin(request) == expected)


@app.middleware("http")
async def browser_csrf_guard(request: Request, call_next: Any) -> Any:
    """Require the configured Mini App origin for browser cookie mutations."""

    if request.method in MUTATING_METHODS and request.url.path.startswith("/api/"):
        has_telegram_init_data = bool(request.headers.get("x-telegram-init-data", "").strip())
        is_auth_route = request.url.path in {"/api/auth/login", "/api/auth/logout"}
        has_browser_cookie = bool(request.cookies.get(BROWSER_SESSION_COOKIE, ""))
        if not has_telegram_init_data and (is_auth_route or has_browser_cookie) and not _is_same_origin_request(request):
            return _no_store({"detail": "Cross-origin request blocked."}, status_code=403)
    return await call_next(request)


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


@app.post("/api/auth/login")
async def browser_login(
    request: Request,
    payload: Dict[str, Any] = Body(...),
) -> JSONResponse:
    """Authenticate a known browser user and issue an opaque session cookie."""

    service = _service()
    repository = getattr(service, "repository", None)
    username = payload.get("username", "")
    password = payload.get("password", "")
    normalized_username: Optional[str] = None
    try:
        normalized_username = normalize_web_username(username)
    except ValueError:
        # Still run the dummy password verifier below so unknown usernames do
        # not have an obviously different verification path.
        pass
    credential = repository.get_web_credential(normalized_username) if normalized_username and repository is not None else None
    password_valid = verify_web_password(password, credential)
    identity = None
    if password_valid and credential is not None and repository is not None:
        try:
            telegram_user_id = int(credential.get("telegram_user_id", 0) or 0)
            identity = repository.get_identity(telegram_user_id)
        except (TypeError, ValueError):
            identity = None
        if identity is None or identity.user_id != str(credential.get("user_id", "") or ""):
            identity = None
    if identity is None:
        logger.warning(
            "browser_login_rejected username_fingerprint=%s client_fingerprint=%s",
            _user_fingerprint(normalized_username or "invalid"),
            _user_fingerprint(getattr(getattr(request, "client", None), "host", "none")),
        )
        return _no_store({"detail": GENERIC_LOGIN_ERROR}, status_code=401)

    token, _session = repository.create_browser_session(identity, ttl_seconds=BROWSER_SESSION_TTL_SECONDS)
    response = _no_store({"authenticated": True, "viewer": _viewer_payload(identity)})
    response.set_cookie(
        BROWSER_SESSION_COOKIE,
        token,
        max_age=BROWSER_SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    logger.info("browser_login_succeeded user_fingerprint=%s", _user_fingerprint(identity.user_id))
    return response


@app.get("/api/auth/session")
async def browser_session(request: Request) -> JSONResponse:
    """Report whether the current browser session is still valid."""

    service = _service()
    try:
        identity = _browser_session_identity(request, service)
    except HTTPException:
        return _no_store({"authenticated": False})
    return _no_store({"authenticated": True, "viewer": _viewer_payload(identity)})


@app.post("/api/auth/logout")
async def browser_logout(request: Request) -> JSONResponse:
    """Revoke the current browser session and expire its cookie."""

    service = _service()
    repository = getattr(service, "repository", None)
    token = request.cookies.get(BROWSER_SESSION_COOKIE, "")
    if token and repository is not None and hasattr(repository, "revoke_browser_session"):
        repository.revoke_browser_session(token)
    response = _no_store({"authenticated": False})
    response.set_cookie(
        BROWSER_SESSION_COOKIE,
        "",
        max_age=0,
        expires=0,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@app.get("/api/profile")
async def get_profile(
    request: Request,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity, launch = _auth_context(request, service)
    result = service.profile_response(identity)
    if launch:
        result["launch_context"] = {
            "launch_type": str(launch.get("launch_type", "nutrition")),
            "requested_day": str(launch.get("requested_day", "") or ""),
        }
    return _no_store(result)


@app.get("/api/workout/programme")
async def get_workout_programme(
    request: Request,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
    version_id: Optional[str] = Query(default=None),
) -> JSONResponse:
    """Return the shared programme; no user execution state is included."""

    service = _service()
    _auth_identity(request, service)
    try:
        result = service.workout_programme(version_id=version_id)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    return _no_store(result)


@app.get("/api/workout/programme/days")
async def get_workout_programme_days(
    request: Request,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
    version_id: Optional[str] = Query(default=None),
) -> JSONResponse:
    service = _service()
    _auth_identity(request, service)
    try:
        programme = service.workout_programme(version_id=version_id)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    return _no_store({"programme": programme["programme"], "version": programme["version"], "days": programme["days"]})


@app.get("/api/workout/programme/days/{day_code}")
async def get_workout_programme_day(
    day_code: str,
    request: Request,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
    version_id: Optional[str] = Query(default=None),
) -> JSONResponse:
    service = _service()
    _auth_identity(request, service)
    try:
        result = service.workout_programme_day(day_code, version_id=version_id)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    return _no_store(result)


def _workout_error(err: Exception) -> HTTPException:
    if isinstance(err, InvalidWorkoutInput):
        return HTTPException(status_code=400, detail=str(err))
    if isinstance(err, WorkoutNotFound):
        return HTTPException(status_code=404, detail=str(err))
    if isinstance(err, WorkoutConflict):
        return HTTPException(status_code=409, detail=str(err))
    return HTTPException(status_code=500, detail="Workout operation failed")


def _choice_changes(
    service: NutritionService,
    identity: Any,
    session_id: str,
    execution_id: str,
    payload: Dict[str, Any],
) -> bool:
    """Return whether the requested choice differs from the current execution."""

    requested = str(payload.get("performed_exercise_id", "")).strip()
    try:
        current = service.workout_session(identity, session_id)
    except WorkoutNotFound:
        # Let the authoritative mutation below return the appropriate 404.
        return True
    for execution in current.get("executions", []):
        if str(execution.get("execution_id", "")) == execution_id:
            return requested != str(execution.get("performed_exercise_id", ""))
    return True


@app.post("/api/workout/sessions")
async def start_workout_session(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(request, service)
    started = time.monotonic()
    try:
        session = service.start_workout(identity, str(payload.get("day_code", "")))
    except (InvalidWorkoutInput, WorkoutNotFound, WorkoutConflict) as err:
        raise _workout_error(err) from err
    _log_workout_api_event(
        "workout_session_created",
        identity,
        stage="session_start",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return _no_store({"session": session})


@app.get("/api/workout/sessions/active")
async def get_active_workout_session(
    request: Request,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(request, service)
    started = time.monotonic()
    try:
        session = service.active_workout(identity)
    except (InvalidWorkoutInput, WorkoutNotFound, WorkoutConflict) as err:
        raise _workout_error(err) from err
    _log_workout_api_event(
        "workout_active_session_read",
        identity,
        stage="active_session_read",
        result="found" if session else "empty",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    if session:
        _log_workout_api_event("workout_session_resumed", identity, stage="session_resume")
    return _no_store({"session": session})


@app.get("/api/workout/sessions/{session_id}")
async def get_workout_session(
    session_id: str,
    request: Request,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(request, service)
    try:
        session = service.workout_session(identity, session_id)
    except (InvalidWorkoutInput, WorkoutNotFound, WorkoutConflict) as err:
        raise _workout_error(err) from err
    return _no_store(session)


@app.put("/api/workout/sessions/{session_id}/executions/{execution_id}")
async def choose_workout_exercise(
    session_id: str,
    execution_id: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(request, service)
    choice_changed = _choice_changes(service, identity, session_id, execution_id, payload)
    started = time.monotonic()
    try:
        session = service.choose_workout_exercise(identity, session_id, execution_id, payload)
    except (InvalidWorkoutInput, WorkoutNotFound, WorkoutConflict) as err:
        raise _workout_error(err) from err
    if choice_changed:
        _log_workout_api_event(
            "workout_exercise_choice_saved",
            identity,
            stage="exercise_choice",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    return _no_store(session)


@app.post("/api/workout/sessions/{session_id}/executions/{execution_id}/skip")
async def skip_workout_exercise(
    session_id: str,
    execution_id: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(request, service)
    started = time.monotonic()
    try:
        session = service.skip_workout_exercise(identity, session_id, execution_id, payload)
    except (InvalidWorkoutInput, WorkoutNotFound, WorkoutConflict) as err:
        raise _workout_error(err) from err
    _log_workout_api_event(
        "workout_exercise_skipped",
        identity,
        stage="exercise_skip",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return _no_store(session)


@app.post("/api/workout/sessions/{session_id}/executions/{execution_id}/reset")
async def reset_workout_exercise(
    session_id: str,
    execution_id: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(request, service)
    try:
        session = service.reset_workout_exercise(identity, session_id, execution_id, payload)
    except (InvalidWorkoutInput, WorkoutNotFound, WorkoutConflict) as err:
        raise _workout_error(err) from err
    return _no_store(session)


@app.put("/api/workout/sessions/{session_id}/executions/{execution_id}/sets/{set_ordinal}")
async def put_workout_set(
    session_id: str,
    execution_id: str,
    set_ordinal: int,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(request, service)
    started = time.monotonic()
    try:
        session = service.put_workout_set(identity, session_id, execution_id, set_ordinal, payload)
    except (InvalidWorkoutInput, WorkoutNotFound, WorkoutConflict) as err:
        raise _workout_error(err) from err
    _log_workout_api_event(
        "workout_set_saved",
        identity,
        stage="set_save",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return _no_store(session)


@app.post("/api/workout/sessions/{session_id}/executions/{execution_id}/sets/{set_ordinal}/skip")
async def skip_workout_set(
    session_id: str,
    execution_id: str,
    set_ordinal: int,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(request, service)
    started = time.monotonic()
    try:
        session = service.skip_workout_set(identity, session_id, execution_id, set_ordinal, payload)
    except (InvalidWorkoutInput, WorkoutNotFound, WorkoutConflict) as err:
        raise _workout_error(err) from err
    _log_workout_api_event(
        "workout_set_saved",
        identity,
        stage="set_skip",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return _no_store(session)


@app.post("/api/workout/sessions/{session_id}/cancel")
async def cancel_workout_session(
    session_id: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(request, service)
    started = time.monotonic()
    try:
        session = service.cancel_workout(identity, session_id, payload)
    except (InvalidWorkoutInput, WorkoutNotFound, WorkoutConflict) as err:
        raise _workout_error(err) from err
    _log_workout_api_event(
        "workout_session_cancelled",
        identity,
        stage="session_cancel",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return _no_store(session)


@app.post("/api/workout/sessions/{session_id}/complete")
async def complete_workout_session(
    session_id: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(request, service)
    started = time.monotonic()
    try:
        session = service.complete_workout(identity, session_id, payload)
    except (InvalidWorkoutInput, WorkoutNotFound, WorkoutConflict) as err:
        raise _workout_error(err) from err
    _log_workout_api_event(
        "workout_session_completed",
        identity,
        stage="session_complete",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return _no_store(session)


@app.post("/api/targets/preview")
async def preview_targets(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    _auth_identity(request)
    try:
        result = NutritionService.preview_payload(payload)
    except InvalidUserInput as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return _no_store(result)


@app.post("/api/profile")
async def save_profile(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(request, service)
    try:
        result = service.save_profile(identity, payload)
    except InvalidUserInput as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return _no_store(result)


@app.get("/api/targets")
async def get_targets(
    request: Request,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(request, service)
    return _no_store({"targets": service.target_history(identity)})


@app.post("/api/targets")
async def save_target(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    """Append a target revision; questionnaire data remains the source of calculation."""

    service = _service()
    identity = _auth_identity(request, service)
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
    request: Request,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
    date_start: Optional[date] = Query(default=None),
    date_end: Optional[date] = Query(default=None),
) -> JSONResponse:
    service = _service()
    identity = _auth_identity(request, service)
    if date_start is not None and date_end is not None and date_end < date_start:
        raise HTTPException(status_code=400, detail="date_end must not precede date_start")
    return _no_store(service.meals_payload(identity, date_start, date_end or date_start))


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def unknown_api_route(path: str) -> JSONResponse:
    return _no_store({"error": "not_found", "path": f"/api/{path}"}, status_code=404)


handler = Mangum(app, lifespan="off")
