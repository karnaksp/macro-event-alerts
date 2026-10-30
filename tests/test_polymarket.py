"""Read-only Polymarket normalization: fake HTTP, real contract checks."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

import macro_event_alerts.polymarket as adapter
from macro_event_alerts.domain import sha256, subject_key
from macro_event_alerts.validation import Contracts

NOW = datetime(2098, 10, 30, 10, 0, tzinfo=timezone.utc)


def metadata():
    return {
        "id": "123456", "version": "v1", "conditionId": "0xsyntheticcondition",
        "question": "Will the synthetic Fed scenario happen?", "description": "Resolves Yes iff the synthetic scenario occurs.",
        "resolutionSource": "https://example.com/criteria", "endDate": "2098-12-09T23:59:00Z",
        "active": True, "closed": False, "acceptingOrders": True,
        "outcomes": '["Yes","No"]', "clobTokenIds": '["111111","222222"]', "liquidityNum": 15000.25,
    }


def book():
    return {
        "asset_id": "111111", "market": "0xsyntheticcondition",
        "timestamp": str(int(NOW.timestamp() * 1000) - 2000),
        "bids": [{"price": "0.40", "size": "10"}, {"price": "0.54", "size": "100"}],
        "asks": [{"price": "0.70", "size": "5"}, {"price": "0.56", "size": "100"}],
    }


def execute(monkeypatch, *, market=None, orderbook=None, outcome="Yes"):
    responses = iter([metadata() if market is None else market, book() if orderbook is None else orderbook])
    calls = []
    def request(url):
        calls.append(url)
        return next(responses)
    monkeypatch.setattr(adapter, "_request_json", request)
    batch = adapter.fetch("polymarket", {"options": {"market_id": "123456", "outcome": outcome}}, now=NOW)
    return batch, calls


def test_two_public_requests_normalise_actual_book_midpoint(monkeypatch):
    batch, calls = execute(monkeypatch)
    assert batch.request_count == 2
    assert len(calls) == 2
    assert calls[0] == "https://gamma-api.polymarket.com/markets/123456"
    assert parse_qs(urlparse(calls[1]).query) == {"token_id": ["111111"]}
    envelope = batch.envelopes[0]
    item = envelope["observation"]
    Contracts().validate(item, "observation.schema.json")
    assert item["raw_sha256"] == sha256(envelope["raw"])
    assert item["values"]["probability"] == "0.55"
    assert item["values"]["best_bid"] == "0.54"
    assert item["values"]["best_ask"] == "0.56"
    assert item["values"]["sample_at"] == "2098-10-30T09:59:58Z"
    assert item["source"]["source_updated_at"] == item["values"]["sample_at"]
    assert item["observed_at"] == "2098-10-30T10:00:00Z"
    assert item["subject"]["outcome_id"] == "111111"
    assert item["subject"]["outcome_label"] == "Yes"
    assert item["values"]["liquidity_usd"] == "15000.25"


@pytest.mark.parametrize("source_label,requested,token", [
    ("Yes", "yes", "111111"),
    ("No", "NO", "222222"),
    ("Custom <&> outcome", "custom <&> outcome", "111111"),
])
def test_outcome_label_matches_selected_token_exactly(monkeypatch, source_label, requested, token):
    market, orderbook = metadata(), book()
    market["outcomes"] = [source_label if token == "111111" else "Yes", "No"]
    orderbook["asset_id"] = token
    batch, calls = execute(monkeypatch, market=market, orderbook=orderbook, outcome=requested)
    item = batch.envelopes[0]["observation"]
    Contracts().validate(item, "observation.schema.json")
    assert item["subject"]["outcome_id"] == token
    assert item["subject"]["outcome_label"] == source_label
    assert parse_qs(urlparse(calls[1]).query) == {"token_id": [token]}


def test_outcome_label_is_optional_and_does_not_change_identity(monkeypatch):
    batch, _ = execute(monkeypatch)
    item = batch.envelopes[0]["observation"]
    legacy = {**item, "subject": dict(item["subject"])}
    legacy["subject"].pop("outcome_label")
    Contracts().validate(legacy, "observation.schema.json")
    assert subject_key(item) == subject_key(legacy)


def test_duplicate_token_cannot_assign_ambiguous_outcome_label(monkeypatch):
    market = metadata()
    market["clobTokenIds"] = '["111111","111111"]'
    with pytest.raises(RuntimeError, match="polymarket_book_invalid"):
        execute(monkeypatch, market=market)


@pytest.mark.parametrize("field,value", [("active", False), ("closed", True), ("acceptingOrders", False)])
def test_closed_or_inactive_market_stops_after_metadata(monkeypatch, field, value):
    market = metadata()
    market[field] = value
    batch, calls = execute(monkeypatch, market=market)
    assert batch.envelopes == []
    assert batch.request_count == len(calls) == 1


@pytest.mark.parametrize("change", [
    {"bids": []}, {"asks": []},
    {"bids": [{"price": "0.90", "size": "1"}]},
    {"asset_id": "222222"}, {"market": "other"},
    {"timestamp": str(int(NOW.timestamp() * 1000) + 1000)},
    {"timestamp": None}, {"timestamp": "NaN"},
    {"bids": [{"price": "0.54", "size": "0"}]},
    {"bids": [{"price": "1.1", "size": "1"}]},
])
def test_invalid_book_cannot_create_observation(monkeypatch, change):
    item = book()
    item.update(change)
    with pytest.raises(RuntimeError, match="polymarket_book_invalid"):
        execute(monkeypatch, orderbook=item)


def test_stale_book_is_not_relabelled_fresh(monkeypatch):
    old = book()
    old["timestamp"] = str(int(NOW.timestamp() * 1000) - 3600000)
    batch, _ = execute(monkeypatch, orderbook=old)
    item = batch.envelopes[0]["observation"]
    assert item["values"]["sample_at"] == "2098-10-30T09:00:00Z"
    assert item["observed_at"] == "2098-10-30T10:00:00Z"


def test_resolution_hash_ignores_changing_liquidity_but_detects_rules(monkeypatch):
    first, _ = execute(monkeypatch)
    changed = metadata()
    changed["liquidityNum"] = 20000
    second, _ = execute(monkeypatch, market=changed)
    assert first.envelopes[0]["observation"]["subject"]["resolution_hash"] == second.envelopes[0]["observation"]["subject"]["resolution_hash"]
    changed["description"] += " Modified resolution."
    third, _ = execute(monkeypatch, market=changed)
    assert first.envelopes[0]["observation"]["subject"]["resolution_hash"] != third.envelopes[0]["observation"]["subject"]["resolution_hash"]


def test_missing_liquidity_remains_null(monkeypatch):
    market = metadata()
    market.pop("liquidityNum")
    batch, _ = execute(monkeypatch, market=market)
    assert batch.envelopes[0]["observation"]["values"]["liquidity_usd"] is None


def test_unconfigured_outcome_is_rejected(monkeypatch):
    market = metadata()
    market["outcomes"] = '["Other","No"]'
    with pytest.raises(RuntimeError, match="metadata_invalid"):
        execute(monkeypatch, market=market)


def test_v2_position_ids_are_not_guessed_as_ctf_token_ids(monkeypatch):
    market = metadata()
    market["version"] = "v2"
    with pytest.raises(RuntimeError, match="metadata_invalid"):
        execute(monkeypatch, market=market)
