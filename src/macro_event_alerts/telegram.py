"""Small Telegram Bot API transport with credential-free delivery outcomes.

Retries and durable state belong to the outbox worker. A timeout may occur after
Telegram accepts a message, so its outcome is ambiguous rather than undelivered.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


Status = Literal["delivered", "retry", "dead_letter", "ambiguous"]


@dataclass(frozen=True)
class SendResult:
    status: Status
    message_id: str | None = None
    error_code: str | None = None
    retry_after_seconds: float | None = None


# The callback receives credentials only for the request; exceptions are never
# exposed outside TelegramSender. Tests inject this callback without networking.
RequestJSON = Callable[[str, dict, float], dict]
_RESPONSE_LIMIT = 1024 * 1024
_TIMEOUT = 15.0


def _request_json(url: str, payload: dict, timeout: float) -> dict:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "macro-event-alerts"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read(_RESPONSE_LIMIT + 1)
    if len(body) > _RESPONSE_LIMIT:
        raise ValueError("response_limit")
    return json.loads(body)


def _seconds(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _short_text(text: str) -> str:
    """Keep at most 4096 UTF-16 units without cutting a Unicode code point."""
    used = 0
    for index, character in enumerate(text):
        used += 2 if ord(character) > 0xFFFF else 1
        if used > 4096:
            shortened = text[:index]
            while sum(2 if ord(ch) > 0xFFFF else 1 for ch in shortened) > 4095:
                shortened = shortened[:-1]
            return shortened + "…"
    return text


def _failure(body: object, http_code: int | None = None, retry_header: object = None) -> SendResult:
    data = body if isinstance(body, dict) else {}
    api_code = data.get("error_code")
    code = http_code if http_code is not None else api_code
    if isinstance(code, bool) or not isinstance(code, int) or not 100 <= code <= 599:
        return SendResult("retry", error_code="telegram_error")
    safe_code = f"telegram_{code}"
    if code == 429:
        parameters = data.get("parameters")
        delay = _seconds(parameters.get("retry_after")) if isinstance(parameters, dict) else None
        if delay is None:
            delay = _seconds(retry_header)
        return SendResult("retry", error_code=safe_code, retry_after_seconds=1.0 if delay is None else delay)
    if 400 <= code < 500:
        return SendResult("dead_letter", error_code=safe_code)
    return SendResult("retry", error_code=safe_code)


def _message_result(body: dict, expected_id: int | None = None) -> SendResult:
    result = body.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        return SendResult("ambiguous", error_code="invalid_response")
    if expected_id is not None and message_id != expected_id:
        return SendResult("ambiguous", error_code="invalid_response")
    return SendResult("delivered", message_id=str(message_id))


class TelegramSender:
    def __init__(self, token: str, chat_id: str, thread_id: str | None = None, *, request: RequestJSON | None = None):
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("Telegram token is missing or malformed")
        if not isinstance(chat_id, str) or not (re.fullmatch(r"-?[0-9]+", chat_id) or re.fullmatch(r"@[A-Za-z0-9_]+", chat_id)):
            raise ValueError("Telegram chat ID is missing or malformed")
        if thread_id not in (None, ""):
            if not isinstance(thread_id, str) or not re.fullmatch(r"[0-9]+", thread_id) or int(thread_id) <= 0:
                raise ValueError("Telegram topic ID must be a positive integer")
        self._base = "https://api.telegram.org/bot" + token + "/"
        self._chat_id = chat_id
        self._thread_id = int(thread_id) if thread_id else None
        self._request = request or _request_json

    @property
    def destination(self) -> str:
        return "telegram:" + self._chat_id + ":" + (str(self._thread_id) if self._thread_id else "")

    def _call(self, method: str, payload: dict) -> tuple[dict | None, SendResult | None]:
        try:
            body = self._request(self._base + method, payload, _TIMEOUT)
        except HTTPError as exc:
            # HTTPError string/repr includes the tokenized URL: never propagate it.
            try:
                raw = exc.read(_RESPONSE_LIMIT + 1)
                body = json.loads(raw) if len(raw) <= _RESPONSE_LIMIT else {}
            except Exception:
                body = {}
            retry_header = exc.headers.get("Retry-After") if exc.headers else None
            return None, _failure(body, exc.code, retry_header)
        except (TimeoutError, URLError, OSError):
            return None, SendResult("ambiguous", error_code="transport_uncertain")
        except (ValueError, TypeError):
            return None, SendResult("ambiguous", error_code="invalid_response")
        except Exception:
            # Injected transports and lower-level libraries must not leak URLs.
            return None, SendResult("ambiguous", error_code="transport_uncertain")
        if not isinstance(body, dict) or not isinstance(body.get("ok"), bool):
            return None, SendResult("ambiguous", error_code="invalid_response")
        if body.get("ok") is not True:
            return None, _failure(body)
        return body, None

    def _text_payload(self, text: str, parse_mode: str | None) -> dict | SendResult:
        if parse_mode is not None and parse_mode != "HTML":
            return SendResult("dead_letter", error_code="invalid_parse_mode")
        if not isinstance(text, str) or not text.strip():
            return SendResult("dead_letter", error_code="invalid_text")
        try:
            text.encode("utf-8")
        except UnicodeError:
            return SendResult("dead_letter", error_code="invalid_text")
        # Bound raw HTML conservatively: slicing could split a tag or entity.
        if parse_mode == "HTML" and sum(2 if ord(ch) > 0xFFFF else 1 for ch in text) > 4096:
            return SendResult("dead_letter", error_code="message_too_long")
        payload = {
            "chat_id": self._chat_id,
            "text": text if parse_mode == "HTML" else _short_text(text),
            "link_preview_options": {"is_disabled": True},
        }
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        return payload

    def send(self, text: str, *, parse_mode: str | None = None) -> SendResult:
        payload = self._text_payload(text, parse_mode)
        if isinstance(payload, SendResult):
            return payload
        if self._thread_id is not None:
            payload["message_thread_id"] = self._thread_id
        body, failure = self._call("sendMessage", payload)
        if failure is not None:
            return failure
        return _message_result(body)

    def edit(self, message_id: str, text: str, *, parse_mode: str | None = "HTML") -> SendResult:
        """Edit a message in this chat without posting a second notification."""
        if not isinstance(message_id, str) or not re.fullmatch(r"[0-9]+", message_id):
            return SendResult("dead_letter", error_code="invalid_message_id")
        try:
            numeric_id = int(message_id)
        except ValueError:
            return SendResult("dead_letter", error_code="invalid_message_id")
        if numeric_id <= 0:
            return SendResult("dead_letter", error_code="invalid_message_id")
        payload = self._text_payload(text, parse_mode)
        if isinstance(payload, SendResult):
            return payload
        payload["message_id"] = numeric_id
        body, failure = self._call("editMessageText", payload)
        if failure is not None:
            return failure
        return _message_result(body, expected_id=numeric_id)

    def check(self) -> dict:
        """Read getMe/getChat without sending; return no token or private chat ID."""
        bot, failure = self._call("getMe", {})
        if failure is not None:
            return {"ok": False, "error_code": failure.error_code}
        bot_info = bot.get("result")
        if not isinstance(bot_info, dict) or bot_info.get("is_bot") is not True:
            return {"ok": False, "error_code": "invalid_response"}
        chat, failure = self._call("getChat", {"chat_id": self._chat_id})
        if failure is not None:
            return {"ok": False, "error_code": failure.error_code}
        chat_info = chat.get("result")
        if not isinstance(chat_info, dict) or chat_info.get("type") not in {"private", "group", "supergroup", "channel"}:
            return {"ok": False, "error_code": "invalid_response"}
        if self._chat_id.lstrip("-").isdigit() and str(chat_info.get("id")) != self._chat_id:
            return {"ok": False, "error_code": "chat_mismatch"}
        result = {"ok": True, "chat_type": chat_info["type"]}
        username = bot_info.get("username")
        if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", username):
            result["bot_username"] = username
        return result
