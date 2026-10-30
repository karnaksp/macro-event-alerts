from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from macro_event_alerts.engine import detect, evaluate_rule

ROOT = Path(__file__).resolve().parents[1]


def macro(at: str = "2026-10-14T12:00:00Z", **values) -> dict:
    observation = json.loads((ROOT / "examples/observations/macro.json").read_text())
    observation["observation_id"] = str(uuid4())
    observation["observed_at"] = at
    observation["source"]["source_updated_at"] = at
    observation["values"].update(values)
    return observation


def scenario(value: str, at: str) -> dict:
    observation = json.loads((ROOT / "examples/observations/scenario.json").read_text())
    observation["observation_id"] = str(uuid4())
    observation["observed_at"] = at
    observation["source"]["source_updated_at"] = at
    observation["values"].update({
        "probability": value,
        "sample_at": at,
        "best_bid": str(Decimal(value) - Decimal("0.01")),
        "best_ask": str(Decimal(value) + Decimal("0.01")),
    })
    return observation


def rule(*, scenario_rule: bool = False) -> dict:
    return {
        "id": "scenario" if scenario_rule else "macro",
        "enabled": True,
        "provider_ids": ["fixture"],
        "event_types": ["scenario.probability_changed"] if scenario_rule else ["macro.release", "macro.forecast_changed", "macro.revised"],
        "series_ids": [] if scenario_rule else ["US.CPI.HEADLINE.YOY"],
        "contract_ids": ["synthetic-fed-meeting-december"] if scenario_rule else [],
        "min_change_pp": "15" if scenario_rule else None,
        "comparison_window_seconds": 3600,
        "cooldown_seconds": 300,
        "max_observation_age_seconds": 300,
        "max_spread_pp": "5" if scenario_rule else None,
        "min_liquidity_usd": "10000" if scenario_rule else None,
    }


def now(text: str = "2026-10-14T13:00:00Z") -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def test_baseline_and_changed_identity_produce_no_events():
    first = macro()
    assert detect(None, first, state_version=1) == []
    other_period = macro("2026-10-14T12:01:00Z", actual="3.2")
    other_period["subject"]["reference_period"] = "2026-10"
    assert detect(first, other_period, state_version=1) == []
    other_source = copy.deepcopy(other_period)
    other_source["subject"] = copy.deepcopy(first["subject"])
    other_source["source"]["provider_id"] = "different-provider"
    assert detect(first, other_source, state_version=1) == []


def test_forecast_null_initializes_and_origin_change_resets_comparison():
    empty = macro(actual=None, publication_status="scheduled", forecast=None)
    first = macro("2026-10-14T12:01:00Z", actual=None, publication_status="scheduled", forecast="3.0")
    assert detect(empty, first, state_version=2) == []
    changed = macro("2026-10-14T12:02:00Z", actual=None, publication_status="scheduled", forecast="3.1")
    assert detect(first, changed, state_version=3)[0]["payload"]["after"] == "3.1"
    changed["values"]["forecast_origin"] = "model"
    assert detect(first, changed, state_version=3) == []


def test_equal_numeric_forecast_and_published_forecast_change_are_suppressed():
    before = macro(actual=None, publication_status="scheduled", forecast="3.0")
    after = macro("2026-10-14T12:01:00Z", actual=None, publication_status="scheduled", forecast="3.00")
    assert detect(before, after, state_version=2) == []
    before = macro(forecast="3.0")
    after = macro("2026-10-14T12:32:00Z", forecast="3.2")
    assert detect(before, after, state_version=2) == []


def test_first_actual_is_release_even_when_equal_to_previous_period_and_zero():
    before = macro(actual=None, publication_status="scheduled")
    after = macro("2026-10-14T12:31:00Z", actual="0", previous="0")
    event, = detect(before, after, state_version=2)
    assert event["event_type"] == "macro.release"
    assert event["payload"]["actual"] == "0"
    assert event["state_version"] == 2


def test_revision_references_original_and_contextual_previous_is_not_actual_revision():
    before = macro()
    after = macro("2026-10-14T12:32:00Z", actual="3.2")
    original_id = str(uuid4())
    event, = detect(before, after, state_version=2, original_release_event_id=original_id)
    assert event["event_type"] == "macro.revised"
    assert event["payload"]["before_actual"] == "3.1"
    assert event["payload"]["original_release_event_id"] == original_id
    after["values"]["actual"] = "3.1"
    after["values"]["previous"] = "2.8"
    assert detect(before, after, state_version=2) == []


def test_scenario_a_b_a_has_distinct_events_and_decimal_delta():
    observations = [
        scenario("0.55", "2026-10-14T12:00:00Z"),
        scenario("0.75", "2026-10-14T12:30:00Z"),
        scenario("0.55", "2026-10-14T13:00:00Z"),
        scenario("0.75", "2026-10-14T13:30:00Z"),
    ]
    events = [detect(left, right, state_version=index + 2)[0] for index, (left, right) in enumerate(zip(observations, observations[1:]))]
    assert len({event["event_id"] for event in events}) == 3
    assert [event["payload"]["delta_pp"] for event in events] == ["20", "-20", "20"]
    assert [event["state_version"] for event in events] == [2, 3, 4]


def test_backfill_closed_market_and_nonincreasing_observation_time_are_suppressed():
    before = scenario("0.55", "2026-10-14T12:00:00Z")
    after = scenario("0.75", "2026-10-14T13:00:00Z")
    after["is_backfill"] = True
    assert detect(before, after, state_version=2) == []
    after["is_backfill"] = False
    after["values"]["market_status"] = "closed"
    assert detect(before, after, state_version=2) == []
    after["values"]["market_status"] = "open"
    after["observed_at"] = before["observed_at"]
    assert detect(before, after, state_version=2) == []


def window_fixture():
    first = scenario("0.55", "2026-10-14T12:00:00Z")
    middle = scenario("0.64", "2026-10-14T12:30:00Z")
    last = scenario("0.75", "2026-10-14T13:00:00Z")
    event, = detect(middle, last, state_version=3)
    return first, middle, last, event


def test_window_evaluation_retains_twenty_pp_when_last_transition_is_eleven():
    first, middle, last, event = window_fixture()
    context, reason = evaluate_rule(rule(scenario_rule=True), event, last, [first, middle, last], now=now(), activated_at=first["observed_at"])
    assert reason == "matched"
    assert event["payload"]["delta_pp"] == "11"
    assert context["delta_pp"] == "20"
    assert context["before"] == "0.55"
    assert context["baseline_observation_id"] == first["observation_id"]
    last["values"]["probability"] = "0.90"
    assert context["after"] == "0.75"  # no reference to mutable observations


@pytest.mark.parametrize(("mutation", "reason"), [
    (("liquidity_usd", None), "unknown_liquidity"),
    (("liquidity_usd", "9999"), "insufficient_liquidity"),
    (("sample_at", "2026-10-14T12:00:00Z"), "stale_sample"),
    (("market_status", "resolved"), "market_not_open"),
    (("best_bid", None), "missing_book"),
    (("probability", "0.90"), "inconsistent_midpoint"),
])
def test_rule_quality_failures_have_specific_reasons(mutation, reason):
    first, middle, last, event = window_fixture()
    key, value = mutation
    last["values"][key] = value
    context, actual = evaluate_rule(rule(scenario_rule=True), event, last, [first, middle], now=now())
    assert context is None
    assert actual == reason


def test_wide_spread_and_cooldown_suppress_without_creating_context():
    first, middle, last, event = window_fixture()
    last["values"].update(best_bid="0.70", best_ask="0.80")
    assert evaluate_rule(rule(scenario_rule=True), event, last, [first, middle], now=now())[1] == "spread_too_wide"
    last["values"].update(best_bid="0.74", best_ask="0.76")
    assert evaluate_rule(rule(scenario_rule=True), event, last, [first, middle], now=now(), last_alert_at="2026-10-14T12:59:00Z")[1] == "cooldown"


def test_window_requires_anchor_after_rule_activation_and_rejects_old_anchor():
    first, middle, last, event = window_fixture()
    rules = rule(scenario_rule=True)
    assert evaluate_rule(rules, event, last, [middle], now=now())[1] == "window_baseline_missing"
    assert evaluate_rule(rules, event, last, [first, middle], now=now(), activated_at=middle["observed_at"])[1] == "window_baseline_missing"
    first["observed_at"] = first["values"]["sample_at"] = "2026-10-13T12:00:00Z"
    assert evaluate_rule(rules, event, last, [first, middle], now=now())[1] == "window_baseline_stale"


def test_macro_release_uses_publication_freshness_but_revision_uses_revision_time():
    before = macro("2026-10-14T12:00:00Z", actual=None, publication_status="scheduled")
    after = macro("2026-10-14T13:00:00Z", published_at="2026-10-14T12:30:00Z")
    event, = detect(before, after, state_version=2)
    assert evaluate_rule(rule(), event, after, [before], now=now())[1] == "stale_source_timestamp"
    revised = macro("2026-10-14T13:01:00Z", actual="3.2", published_at="2026-10-14T12:30:00Z")
    event, = detect(after, revised, state_version=3)
    assert evaluate_rule(rule(), event, revised, [before, after], now=now("2026-10-14T13:01:00Z"))[1] == "matched"


def test_macro_rule_ignores_foreign_series_and_stale_observation():
    before = macro("2026-10-14T12:00:00Z", actual=None, publication_status="scheduled")
    after = macro("2026-10-14T12:31:00Z", published_at="2026-10-14T12:30:00Z")
    event, = detect(before, after, state_version=2)
    assert evaluate_rule(rule(), event, after, [before], now=now())[1] == "stale_observation"
    config = rule()
    config["series_ids"] = ["different-series"]
    assert evaluate_rule(config, event, after, [before], now=now("2026-10-14T12:31:00Z"))[1] == "series_mismatch"
