"""Public Gamma metadata + CLOB midpoint adapter; no credentials or trading API."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from .domain import decimal_text, instant, sha256, utc


@dataclass(frozen=True)
class FetchBatch:
    envelopes: list[dict]
    checkpoint: dict | None = None
    request_count: int = 2


def capabilities(adapter: str) -> set[str]:
    return {"scenario.probability_changed"} if adapter == "polymarket" else set()


def _request_json(url: str) -> dict:
    try:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "macro-event-alerts"})
        with urlopen(request, timeout=15) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("response_limit")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("response_not_object")
        return result
    except Exception:
        raise RuntimeError("polymarket_request_failed") from None


def _number(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("invalid_number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid_number") from None
    if not result.is_finite():
        raise ValueError("invalid_number")
    return result


def _array(value: object) -> list:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("invalid_array")
    return value


def _book_time(value: object) -> datetime:
    milliseconds = _number(value)
    if milliseconds < 0 or milliseconds != milliseconds.to_integral_value():
        raise ValueError("invalid_book_timestamp")
    # Decimal/integer arithmetic retains milliseconds without floating-point time.
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(milliseconds))


def _best(levels: object, *, bid: bool) -> Decimal:
    if not isinstance(levels, list):
        raise ValueError("invalid_book_levels")
    prices = []
    for level in levels:
        if not isinstance(level, dict):
            raise ValueError("invalid_book_level")
        price, size = _number(level.get("price")), _number(level.get("size"))
        if not 0 <= price <= 1 or size < 0:
            raise ValueError("invalid_book_level")
        if size > 0:
            prices.append(price)
    if not prices:
        raise ValueError("empty_book")
    return max(prices) if bid else min(prices)


def _token(metadata: dict, requested_outcome: str) -> str:
    # This adapter is for CTF/CLOB v1. V2 position identifiers need their own
    # verified endpoint contract rather than being guessed as token identifiers.
    if metadata.get("version") not in (None, "v1"):
        raise ValueError("unsupported_market_version")
    outcomes, token_ids = _array(metadata.get("outcomes")), _array(metadata.get("clobTokenIds"))
    matches = [i for i, label in enumerate(outcomes) if isinstance(label, str) and label.casefold() == requested_outcome.casefold()]
    if len(matches) != 1 or len(outcomes) != len(token_ids):
        raise ValueError("unknown_or_ambiguous_outcome")
    token = token_ids[matches[0]]
    if not isinstance(token, str) or not re.fullmatch(r"[0-9]{1,100}", token):
        raise ValueError("invalid_token_id")
    return token


def _normalise(provider_id: str, metadata: dict, book: dict, token: str, *, now: datetime) -> dict:
    if not isinstance(book, dict) or str(book.get("asset_id")) != token:
        raise ValueError("book_asset_mismatch")
    condition = metadata.get("conditionId")
    if not isinstance(condition, str) or not condition or book.get("market") != condition:
        raise ValueError("book_market_mismatch")
    sample_at = _book_time(book.get("timestamp"))
    if sample_at > now:
        raise ValueError("future_book_timestamp")
    bid, ask = _best(book.get("bids"), bid=True), _best(book.get("asks"), bid=False)
    if bid > ask:
        raise ValueError("crossed_book")
    question, description = metadata.get("question"), metadata.get("description")
    if not isinstance(question, str) or not question or not isinstance(description, str) or not description:
        raise ValueError("missing_resolution_criteria")
    end = instant(metadata["endDate"])
    if end <= now:
        raise ValueError("expired_market")
    market_id = str(metadata["id"])
    resolution = {
        key: metadata.get(key)
        for key in ("question", "description", "resolutionSource", "endDate", "conditionId")
    }
    liquidity = None
    if metadata.get("liquidityNum") is not None:
        parsed = _number(metadata["liquidityNum"])
        if parsed < 0:
            raise ValueError("invalid_liquidity")
        liquidity = decimal_text(parsed)
    # liquidityNum is market-aggregate liquidity, not executable size of this
    # outcome. It remains explicit metadata; both sides of the actual book are required.
    source_url = "https://gamma-api.polymarket.com/markets/" + market_id
    raw = {"metadata": metadata, "book": book}
    observation = {
        "schema_version": "1.0",
        "observation_id": str(uuid4()),
        "source": {
            "provider_id": provider_id,
            "source_record_id": market_id + ":" + token,
            "source_revision": None,
            "source_url": source_url,
            "source_updated_at": utc(sample_at),
            "declared_delay_seconds": None,
        },
        "observed_at": utc(now),
        "raw_sha256": sha256(raw),
        "is_backfill": False,
        "subject": {
            "kind": "scenario",
            "contract_id": market_id,
            "outcome_id": token,
            "label": question,
            "horizon_at": utc(end),
            "resolution_url": source_url,
            "resolution_hash": sha256(resolution),
            "price_basis": "midpoint",
        },
        "values": {
            "probability": decimal_text((bid + ask) / 2),
            "sample_at": utc(sample_at),
            "best_bid": decimal_text(bid),
            "best_ask": decimal_text(ask),
            "liquidity_usd": liquidity,
            "market_status": "open",
        },
    }
    return {"raw": raw, "observation": observation}


def fetch(provider_id: str, settings: dict, *, now: datetime, checkpoint: dict | None = None) -> FetchBatch:
    """Fetch one configured CTF outcome with exactly two requests on success.

    The CLOB timestamp is not replaced with polling time. An old book is kept
    old, allowing the existing rule freshness filter to suppress it.
    """
    options = settings.get("options", {})
    market_id = str(options.get("market_id", ""))
    outcome = options.get("outcome", "Yes")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", market_id) or not isinstance(outcome, str) or not outcome:
        raise RuntimeError("polymarket_market_configuration_invalid")
    metadata = _request_json("https://gamma-api.polymarket.com/markets/" + market_id)
    try:
        if str(metadata.get("id")) != market_id:
            raise ValueError("market_id_mismatch")
        if metadata.get("active") is not True or metadata.get("closed") is not False or metadata.get("acceptingOrders") is not True:
            return FetchBatch([], checkpoint, request_count=1)
        token = _token(metadata, outcome)
        if instant(metadata["endDate"]) <= now:
            return FetchBatch([], checkpoint, request_count=1)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise RuntimeError("polymarket_metadata_invalid") from None
    book = _request_json("https://clob.polymarket.com/book?" + urlencode({"token_id": token}))
    # Request-start `now` can precede the book timestamp by network latency;
    # receipt time is observed_at, while sample_at remains the source timestamp.
    receipt = max(now, datetime.now(timezone.utc))
    try:
        envelope = _normalise(provider_id, metadata, book, token, now=receipt)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise RuntimeError("polymarket_book_invalid") from None
    return FetchBatch([envelope], checkpoint)
