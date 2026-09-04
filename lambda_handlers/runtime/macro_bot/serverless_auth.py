"""Telegram Mini App authentication and SSM-backed secret access.

The module deliberately keeps secrets inside the process and never includes
them in exceptions or structured logs.  The pure validator is also usable by
local tests without AWS credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import binascii
import os
import base64
import unicodedata
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qsl

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_SECONDS = 3600
DEFAULT_FUTURE_SKEW_SECONDS = 60
WEB_PASSWORD_ALGORITHM = "pbkdf2_sha256"
WEB_PASSWORD_VERSION = 1
WEB_PASSWORD_ITERATIONS = 600_000
WEB_PASSWORD_SALT_BYTES = 16
WEB_PASSWORD_HASH_BYTES = 32
WEB_PASSWORD_MIN_LENGTH = 12
WEB_PASSWORD_MAX_LENGTH = 1024
WEB_USERNAME_MAX_LENGTH = 128


def normalize_web_username(username: Any) -> str:
    """Return the canonical username used for browser credential lookup."""

    value = unicodedata.normalize("NFKC", str(username or "")).strip().casefold()
    if not value or len(value) > WEB_USERNAME_MAX_LENGTH:
        raise ValueError("username is invalid")
    return value


def web_credential_key(username: Any) -> str:
    """Return a non-reversible partition key for a browser username."""

    normalized = normalize_web_username(username)
    digest = hashlib.sha256(f"javaanfitness-web-credential-v1\0{normalized}".encode("utf-8")).hexdigest()
    return f"WEB_CREDENTIAL#{digest}"


def browser_session_token_hash(token: Any) -> str:
    """Hash an opaque browser token before it is used as a DynamoDB key."""

    value = str(token or "")
    return hashlib.sha256(f"javaanfitness-browser-session-v1\0{value}".encode("utf-8")).hexdigest()


def _validate_password(password: Any) -> str:
    if not isinstance(password, str):
        raise ValueError("password is invalid")
    if not WEB_PASSWORD_MIN_LENGTH <= len(password) <= WEB_PASSWORD_MAX_LENGTH:
        raise ValueError("password must be between 12 and 1024 characters")
    return password


def hash_web_password(password: str, *, iterations: int = WEB_PASSWORD_ITERATIONS) -> dict[str, Any]:
    """Hash a browser password with a random salt and explicit KDF metadata."""

    password = _validate_password(password)
    iterations = int(iterations)
    if iterations < 100_000:
        raise ValueError("password KDF iteration count is too low")
    salt = os.urandom(WEB_PASSWORD_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=WEB_PASSWORD_HASH_BYTES,
    )
    return {
        "password_algorithm": WEB_PASSWORD_ALGORITHM,
        "password_version": WEB_PASSWORD_VERSION,
        "password_iterations": iterations,
        "password_salt": base64.b64encode(salt).decode("ascii"),
        "password_hash": base64.b64encode(derived).decode("ascii"),
    }


def _dummy_password_record() -> dict[str, Any]:
    """Provide a valid work factor for unknown usernames without a secret."""

    return {
        "password_algorithm": WEB_PASSWORD_ALGORITHM,
        "password_version": WEB_PASSWORD_VERSION,
        "password_iterations": WEB_PASSWORD_ITERATIONS,
        "password_salt": base64.b64encode(b"javaanfitness-dummy-salt").decode("ascii"),
        "password_hash": base64.b64encode(b"\0" * WEB_PASSWORD_HASH_BYTES).decode("ascii"),
    }


def verify_web_password(password: Any, credential: Optional[Mapping[str, Any]]) -> bool:
    """Verify a password without revealing whether the username exists."""

    password_text = password if isinstance(password, str) else ""
    record = dict(credential or _dummy_password_record())
    try:
        if record.get("password_algorithm") != WEB_PASSWORD_ALGORITHM:
            return False
        if int(record.get("password_version", 0)) != WEB_PASSWORD_VERSION:
            return False
        iterations = int(record["password_iterations"])
        if iterations < 100_000 or iterations > 10_000_000:
            return False
        salt = base64.b64decode(str(record["password_salt"]), validate=True)
        expected = base64.b64decode(str(record["password_hash"]), validate=True)
        if len(salt) < 16 or len(expected) != WEB_PASSWORD_HASH_BYTES:
            return False
    except (KeyError, TypeError, ValueError, binascii.Error):
        return False
    if len(password_text) > WEB_PASSWORD_MAX_LENGTH:
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password_text.encode("utf-8"),
        salt,
        iterations,
        dklen=len(expected),
    )
    return hmac.compare_digest(derived, expected)


class TelegramAuthError(ValueError):
    """Raised when Telegram init data cannot be trusted."""


@dataclass(frozen=True)
class TelegramUser:
    telegram_user_id: int
    username: str
    display_name: str
    start_param: str = ""
    chat_type: str = ""
    chat_instance: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "telegram_user_id": self.telegram_user_id,
            "username": self.username,
            "display_name": self.display_name,
        }


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    now: Optional[int] = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    future_skew_seconds: int = DEFAULT_FUTURE_SKEW_SECONDS,
) -> TelegramUser:
    """Validate Telegram Web App ``initData`` and return only its user data.

    Telegram's HMAC data-check string is built from decoded, sorted query
    fields.  The timestamp checks are explicit so a correctly signed but old
    or implausibly future payload is still rejected.
    """

    raw = (init_data or "").strip()
    if not raw:
        raise TelegramAuthError("Missing X-Telegram-Init-Data header")
    if not bot_token:
        raise TelegramAuthError("Telegram authentication is not configured")

    parsed_pairs = parse_qsl(raw, keep_blank_values=True)
    parsed: dict[str, str] = {}
    for key, value in parsed_pairs:
        if key in parsed:
            raise TelegramAuthError("Telegram init data contains duplicate fields")
        parsed[key] = value

    received_hash = parsed.pop("hash", "")
    auth_date_text = parsed.get("auth_date", "")
    user_payload = parsed.get("user", "")
    if not received_hash or not auth_date_text or not user_payload:
        raise TelegramAuthError("Telegram init data is incomplete")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise TelegramAuthError("Telegram init data signature is invalid")

    try:
        auth_timestamp = int(auth_date_text)
    except (TypeError, ValueError) as err:
        raise TelegramAuthError("Telegram init data auth_date is invalid") from err

    current_timestamp = int(time.time() if now is None else now)
    if auth_timestamp > current_timestamp + int(future_skew_seconds):
        raise TelegramAuthError("Telegram init data is from the future")
    if current_timestamp - auth_timestamp > int(max_age_seconds):
        raise TelegramAuthError("Telegram init data is stale")

    try:
        user = json.loads(user_payload)
    except (TypeError, ValueError) as err:
        raise TelegramAuthError("Telegram init data user payload is invalid") from err
    if not isinstance(user, Mapping):
        raise TelegramAuthError("Telegram init data user payload is invalid")

    try:
        telegram_user_id = int(user.get("id", 0) or 0)
    except (TypeError, ValueError) as err:
        raise TelegramAuthError("Telegram init data user id is invalid") from err
    if telegram_user_id <= 0:
        raise TelegramAuthError("Telegram init data user id is invalid")

    username = str(user.get("username", "") or "").strip()
    first_name = str(user.get("first_name", "") or "").strip()
    last_name = str(user.get("last_name", "") or "").strip()
    display_name = " ".join(part for part in (first_name, last_name) if part).strip()
    display_name = display_name or username or str(telegram_user_id)
    return TelegramUser(
        telegram_user_id,
        username,
        display_name,
        start_param=str(parsed.get("start_param", "") or "").strip(),
        chat_type=str(parsed.get("chat_type", "") or "").strip(),
        chat_instance=str(parsed.get("chat_instance", "") or "").strip(),
    )


class SSMParameterCache:
    """Small warm-Lambda cache for SecureString parameters."""

    def __init__(self, client: Any = None):
        self._client = client
        self._values: dict[str, str] = {}

    def get(self, name: str) -> str:
        parameter_name = (name or "").strip()
        if not parameter_name:
            raise RuntimeError("SSM parameter name is not configured")
        if parameter_name in self._values:
            return self._values[parameter_name]
        client = self._client
        if client is None:
            import boto3

            client = boto3.client("ssm")
        result = client.get_parameter(Name=parameter_name, WithDecryption=True)
        value = str(result["Parameter"]["Value"])
        if not value:
            raise RuntimeError("SSM parameter is empty")
        self._values[parameter_name] = value
        return value


def configured_parameter_cache() -> SSMParameterCache:
    return SSMParameterCache()


def bot_token_from_environment(cache: Optional[SSMParameterCache] = None) -> str:
    parameter_name = os.getenv("BOT_TOKEN_PARAMETER_NAME", "").strip()
    return (cache or configured_parameter_cache()).get(parameter_name)


def openai_key_from_environment(cache: Optional[SSMParameterCache] = None) -> str:
    parameter_name = os.getenv("OPENAI_KEY_PARAMETER_NAME", "").strip()
    return (cache or configured_parameter_cache()).get(parameter_name)
