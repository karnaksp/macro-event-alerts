"""Telegram outcomes are tested with injected transports, never real credentials."""
from __future__ import annotations

from dataclasses import asdict
from email.message import Message
import io
import json
import socket
from urllib.error import HTTPError, URLError

import pytest

from macro_event_alerts.telegram import TelegramSender

TOKEN = "123456789:synthetic_offline_token"
CHAT_ID = "-1000000000000"


def sender(request):
    return TelegramSender(TOKEN, CHAT_ID, request=request)


def test_success_is_confirmed_by_ok_and_message_id():
    calls = []
    def request(url, payload, timeout):
        calls.append((url, payload, timeout))
        return {"ok": True, "result": {"message_id": 42}}
    result = sender(request).send("CPI: 3.1% → 3.2%")
    assert result.status == "delivered"
    assert result.message_id == "42"
    assert calls[0][0].endswith("/sendMessage")
    assert calls[0][1] == {
        "chat_id": CHAT_ID,
        "text": "CPI: 3.1% → 3.2%",
        "link_preview_options": {"is_disabled": True},
    }
    assert calls[0][2] > 0


def test_plain_text_topic_and_unicode_truncation():
    calls = []
    def request(url, payload, timeout):
        calls.append(payload)
        return {"ok": True, "result": {"message_id": 1}}
    client = TelegramSender(TOKEN, CHAT_ID, "7", request=request)
    assert client.send("🔥" * 5000).status == "delivered"
    payload = calls[0]
    assert len(payload["text"].encode("utf-16-le")) // 2 <= 4096
    assert payload["text"].endswith("…")
    assert payload["message_thread_id"] == 7
    assert "parse_mode" not in payload
    assert payload["link_preview_options"] == {"is_disabled": True}


def test_html_is_preserved_with_preview_disabled():
    calls = []
    def request(url, payload, timeout):
        calls.append(payload)
        return {"ok": True, "result": {"message_id": 1}}
    text = '<b>CPI &amp; PCE</b>\n<a href="https://example.com/?a=1&amp;b=2">Источник</a>'
    result = sender(request).send(text, parse_mode="HTML")
    assert result.status == "delivered"
    assert calls == [{
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }]


@pytest.mark.parametrize("method", ["send", "edit"])
@pytest.mark.parametrize("parse_mode", ["MarkdownV2", "html", "", True, {}])
def test_invalid_parse_mode_is_rejected_before_transport(method, parse_mode):
    def request(*args):
        pytest.fail("invalid parse mode attempted network")
    client = sender(request)
    args = ("1", "test") if method == "edit" else ("test",)
    result = getattr(client, method)(*args, parse_mode=parse_mode)
    assert result.status == "dead_letter"
    assert result.error_code == "invalid_parse_mode"


@pytest.mark.parametrize("text", ["<b>" + "a" * 4090 + "</b>", "🔥" * 2049, "&amp;" * 820])
@pytest.mark.parametrize("method", ["send", "edit"])
def test_oversized_html_is_rejected_instead_of_cutting_markup(text, method):
    def request(*args):
        pytest.fail("oversized HTML attempted network")
    client = sender(request)
    args = ("1", text) if method == "edit" else (text,)
    result = getattr(client, method)(*args, parse_mode="HTML")
    assert result.status == "dead_letter"
    assert result.error_code == "message_too_long"


def test_html_at_conservative_raw_limit_is_preserved():
    calls = []
    def request(url, payload, timeout):
        calls.append(payload)
        return {"ok": True, "result": {"message_id": 1}}
    text = "<b>" + "a" * 4089 + "</b>"
    assert len(text) == 4096
    assert sender(request).send(text, parse_mode="HTML").status == "delivered"
    assert calls[0]["text"] == text


def test_edit_updates_same_message_with_html_and_no_preview():
    calls = []
    def request(url, payload, timeout):
        calls.append((url.rsplit("/", 1)[-1], payload))
        return {"ok": True, "result": {"message_id": 42}}
    client = TelegramSender(TOKEN, CHAT_ID, "7", request=request)
    result = client.edit("42", "<b>Мониторинг включён</b>")
    assert result.status == "delivered"
    assert result.message_id == "42"
    assert calls == [("editMessageText", {
        "chat_id": CHAT_ID,
        "message_id": 42,
        "text": "<b>Мониторинг включён</b>",
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    })]


@pytest.mark.parametrize("message_id", ["0", "-1", "", "4.2", None, 42, True])
def test_invalid_edit_message_id_is_rejected_locally(message_id):
    def request(*args):
        pytest.fail("invalid message ID attempted network")
    result = sender(request).edit(message_id, "test")
    assert result.status == "dead_letter"
    assert result.error_code == "invalid_message_id"


@pytest.mark.parametrize("result", [True, None, {}, {"message_id": 43}, {"message_id": True}])
def test_edit_requires_matching_message_in_success_response(result):
    outcome = sender(lambda *args: {"ok": True, "result": result}).edit("42", "test")
    assert outcome.status == "ambiguous"
    assert outcome.error_code == "invalid_response"


def test_edit_reuses_safe_api_failure_and_retry_handling():
    client = sender(lambda *args: {
        "ok": False, "error_code": 429,
        "parameters": {"retry_after": 23}, "description": TOKEN,
    })
    result = client.edit("42", "test")
    assert result.status == "retry"
    assert result.retry_after_seconds == 23
    assert TOKEN not in repr(result)


def test_edit_can_use_plain_text_and_handles_not_modified_as_permanent_error():
    calls = []
    def request(url, payload, timeout):
        calls.append(payload)
        return {"ok": False, "error_code": 400, "description": "message is not modified"}
    result = sender(request).edit("42", "<literal>", parse_mode=None)
    assert result.status == "dead_letter"
    assert result.error_code == "telegram_400"
    assert "parse_mode" not in calls[0]
    assert calls[0]["text"] == "<literal>"


@pytest.mark.parametrize("code,status", [(400, "dead_letter"), (401, "dead_letter"), (403, "dead_letter"), (500, "retry"), (503, "retry")])
def test_ok_false_never_counts_as_delivered(code, status):
    result = sender(lambda *args: {"ok": False, "error_code": code, "description": TOKEN}).send("test")
    assert result.status == status
    assert result.error_code == f"telegram_{code}"
    assert TOKEN not in repr(result)


@pytest.mark.parametrize("delay", [0, 12, 120.5])
def test_429_uses_retry_after_seconds(delay):
    result = sender(lambda *args: {"ok": False, "error_code": 429, "parameters": {"retry_after": delay}}).send("test")
    assert result.status == "retry"
    assert result.retry_after_seconds == delay


def test_http_error_is_decoded_and_tokenized_url_not_returned():
    def request(url, payload, timeout):
        raise HTTPError(url, 429, "contains " + TOKEN, Message(), io.BytesIO(b'{"ok":false,"error_code":429,"parameters":{"retry_after":23}}'))
    result = sender(request).send("test")
    assert result.status == "retry"
    assert result.retry_after_seconds == 23
    assert TOKEN not in repr(result)


def test_retry_after_header_used_when_json_is_unavailable():
    def request(url, payload, timeout):
        headers = Message()
        headers["Retry-After"] = "30"
        raise HTTPError(url, 429, TOKEN, headers, io.BytesIO(b"not json"))
    assert sender(request).send("test").retry_after_seconds == 30


@pytest.mark.parametrize("exc", [TimeoutError(TOKEN), socket.timeout(TOKEN), URLError(TOKEN), ConnectionResetError(TOKEN), RuntimeError(TOKEN)])
def test_uncertain_transport_does_not_claim_failure_or_leak_credentials(exc):
    def request(*args):
        raise exc
    result = sender(request).send("test")
    assert result.status == "ambiguous"
    assert result.error_code == "transport_uncertain"
    assert TOKEN not in json.dumps(asdict(result))


@pytest.mark.parametrize("body", [None, [], {}, {"ok": "true"}, {"ok": 1}, {"ok": True}, {"ok": True, "result": {}}, {"ok": True, "result": {"message_id": True}}])
def test_malformed_success_is_ambiguous(body):
    result = sender(lambda *args: body).send("test")
    assert result.status == "ambiguous"
    assert result.error_code == "invalid_response"


@pytest.mark.parametrize("text", ["", "   ", None, "\ud800"])
def test_invalid_text_does_not_call_transport(text):
    def request(*args):
        pytest.fail("invalid text attempted network")
    assert sender(request).send(text).status == "dead_letter"


@pytest.mark.parametrize("bad", [-1, None, "not a number", float("nan"), float("inf"), True])
def test_invalid_retry_after_has_bounded_fallback(bad):
    result = sender(lambda *args: {"ok": False, "error_code": 429, "parameters": {"retry_after": bad}}).send("test")
    assert result.retry_after_seconds == 1


def test_check_reads_bot_and_chat_without_echoing_private_destination():
    methods = []
    def request(url, payload, timeout):
        methods.append(url.rsplit("/", 1)[-1])
        if methods[-1] == "getMe":
            return {"ok": True, "result": {"is_bot": True, "username": "synthetic_bot"}}
        return {"ok": True, "result": {"id": int(CHAT_ID), "type": "supergroup", "title": "private title"}}
    result = sender(request).check()
    assert result == {"ok": True, "chat_type": "supergroup", "bot_username": "synthetic_bot"}
    assert methods == ["getMe", "getChat"]
    assert CHAT_ID not in repr(result)


def test_check_propagates_only_safe_error_code():
    assert sender(lambda *args: {"ok": False, "error_code": 401, "description": TOKEN}).check() == {"ok": False, "error_code": "telegram_401"}
