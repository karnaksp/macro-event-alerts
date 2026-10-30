"""Pure state transitions and rule evaluation; persistence belongs to ingestion."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from .domain import decimal_text, instant, subject_key, utc


def _different(before: str | None, after: str | None) -> bool:
    return before is not None and after is not None and Decimal(before) != Decimal(after)


def _event(observation: dict, event_type: str, state_version: int, payload: dict) -> dict:
    return {
        "schema_version": "1.0",
        "event_id": str(uuid4()),
        "event_type": event_type,
        "subject_key": subject_key(observation),
        "state_version": state_version,
        "source": copy.deepcopy(observation["source"]),
        "observed_at": observation["observed_at"],
        "observation_id": observation["observation_id"],
        "subject": copy.deepcopy(observation["subject"]),
        "is_backfill": observation["is_backfill"],
        "payload": payload,
    }


def detect(
    previous: dict | None,
    observation: dict,
    *,
    state_version: int,
    original_release_event_id: str | None = None,
) -> list[dict]:
    """Detect changes between validated live observations of one subject.

    Ingestion owns ordering, duplicate suppression and transaction boundaries.
    Revisions of another reference period require that period's actual observation;
    a changed contextual `previous` field alone cannot identify its release key.
    """
    if previous is None or observation["is_backfill"]:
        return []
    if subject_key(previous) != subject_key(observation):
        return []
    if instant(observation["observed_at"]) <= instant(previous["observed_at"]):
        return []
    before, after = previous["values"], observation["values"]
    times = {
        "before_observed_at": previous["observed_at"],
        "after_observed_at": observation["observed_at"],
    }
    if observation["subject"]["kind"] == "scenario":
        if before["market_status"] != "open" or after["market_status"] != "open":
            return []
        if not _different(before["probability"], after["probability"]):
            return []
        payload = {
            "before": before["probability"],
            "after": after["probability"],
            "delta_pp": decimal_text((Decimal(after["probability"]) - Decimal(before["probability"])) * 100),
            **times,
            "before_sample_at": before["sample_at"],
            "after_sample_at": after["sample_at"],
            "best_bid": after["best_bid"],
            "best_ask": after["best_ask"],
            "liquidity_usd": after["liquidity_usd"],
        }
        return [_event(observation, "scenario.probability_changed", state_version, payload)]

    published_before = before["publication_status"] == "published" and before["actual"] is not None
    published_after = after["publication_status"] == "published" and after["actual"] is not None
    if published_after and not published_before:
        payload = {
            key: after[key]
            for key in (
                "actual", "forecast", "previous", "previous_reference_period",
                "forecast_origin", "scheduled_at", "published_at",
            )
        }
        return [_event(observation, "macro.release", state_version, payload)]
    if published_before and published_after and _different(before["actual"], after["actual"]):
        return [_event(observation, "macro.revised", state_version, {
            "before_actual": before["actual"],
            "after_actual": after["actual"],
            **times,
            "original_release_event_id": original_release_event_id,
        })]
    if (
        before["publication_status"] == after["publication_status"] == "scheduled"
        and before["actual"] is None and after["actual"] is None
        and before["forecast_origin"] == after["forecast_origin"]
        and _different(before["forecast"], after["forecast"])
    ):
        return [_event(observation, "macro.forecast_changed", state_version, {
            "before": before["forecast"],
            "after": after["forecast"],
            "forecast_origin": after["forecast_origin"],
            **times,
            "scheduled_at": after["scheduled_at"],
        })]
    return []


def _scenario_quality(rule: dict, observation: dict, at: datetime) -> str | None:
    values = observation["values"]
    if values["market_status"] != "open":
        return "market_not_open"
    if instant(observation["subject"]["horizon_at"]) <= at:
        return "scenario_expired"
    age = (at - instant(values["sample_at"])).total_seconds()
    if age < 0:
        return "future_sample"
    if age > rule["max_observation_age_seconds"]:
        return "stale_sample"
    bid, ask = values["best_bid"], values["best_ask"]
    if observation["subject"]["price_basis"] == "midpoint":
        if bid is None or ask is None:
            return "missing_book"
        if Decimal(values["probability"]) != (Decimal(bid) + Decimal(ask)) / 2:
            return "inconsistent_midpoint"
    if bid is not None and ask is not None and Decimal(bid) > Decimal(ask):
        return "crossed_book"
    if rule["max_spread_pp"] is not None:
        if bid is None or ask is None:
            return "missing_book"
        if (Decimal(ask) - Decimal(bid)) * 100 > Decimal(rule["max_spread_pp"]):
            return "spread_too_wide"
    if rule["min_liquidity_usd"] is not None:
        if values["liquidity_usd"] is None:
            return "unknown_liquidity"
        if Decimal(values["liquidity_usd"]) < Decimal(rule["min_liquidity_usd"]):
            return "insufficient_liquidity"
    return None


def evaluate_rule(
    rule: dict,
    event: dict,
    observation: dict,
    history: list[dict],
    *,
    now: datetime,
    last_alert_at: str | None = None,
    activated_at: str | None = None,
) -> tuple[dict | None, str]:
    """Return an immutable evaluation context, or a diagnostic suppression reason.

    `now` is the replay clock during replay and wall clock for future live input.
    History is accepted live observations; neither this function nor the context
    reads mutable state after the transaction that creates an outbox item.
    """
    if not rule["enabled"]:
        return None, "rule_disabled"
    if event["source"]["provider_id"] not in rule["provider_ids"]:
        return None, "provider_mismatch"
    if event["event_type"] not in rule["event_types"]:
        return None, "event_type_mismatch"
    if event["subject_key"] != subject_key(observation):
        return None, "subject_mismatch"
    if observation["is_backfill"]:
        return None, "backfill"
    subject = event["subject"]
    if subject["kind"] == "macro" and subject["series_id"] not in rule["series_ids"]:
        return None, "series_mismatch"
    if subject["kind"] == "scenario" and subject["contract_id"] not in rule["contract_ids"]:
        return None, "contract_mismatch"
    observed_at = instant(observation["observed_at"])
    age = (now - observed_at).total_seconds()
    if age < 0:
        return None, "future_observation"
    if age > rule["max_observation_age_seconds"]:
        return None, "stale_observation"
    if activated_at is not None and observed_at <= instant(activated_at):
        return None, "rule_baseline"
    if last_alert_at is not None:
        if (now - instant(last_alert_at)).total_seconds() < rule["cooldown_seconds"]:
            return None, "cooldown"
    version = hashlib.sha256(json.dumps(rule, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    context = {
        "rule_version": version,
        "mode": "event",
        "current_observation_id": observation["observation_id"],
        "baseline_observation_id": None,
        "window_start": event["payload"].get("before_observed_at", observation["observed_at"]),
        "window_end": observation["observed_at"],
    }
    if subject["kind"] != "scenario":
        payload = event["payload"]
        source_time = (
            payload.get("published_at")
            if event["event_type"] == "macro.release"
            else event["source"]["source_updated_at"]
        )
        if source_time is not None:
            source_age = (now - instant(source_time)).total_seconds()
            if source_age < 0:
                return None, "future_source_timestamp"
            if source_age > rule["max_observation_age_seconds"]:
                return None, "stale_source_timestamp"
        before = payload.get("before", payload.get("before_actual"))
        after = payload.get("after", payload.get("after_actual", payload.get("actual")))
        return {**context, "before": before, "after": after, "delta_pp": None}, "matched"

    quality = _scenario_quality(rule, observation, now)
    if quality is not None:
        return None, quality
    window_start = now - timedelta(seconds=rule["comparison_window_seconds"])
    candidates = [
        item for item in history
        if not item["is_backfill"]
        and subject_key(item) == event["subject_key"]
        and instant(item["observed_at"]) <= window_start
        and (activated_at is None or instant(item["observed_at"]) >= instant(activated_at))
        and _scenario_quality(rule, item, instant(item["observed_at"])) is None
    ]
    if not candidates:
        return None, "window_baseline_missing"
    baseline = max(candidates, key=lambda item: instant(item["observed_at"]))
    # An old but once-fresh quote cannot silently stretch a one-hour window to days.
    if (window_start - instant(baseline["observed_at"])).total_seconds() > rule["max_observation_age_seconds"]:
        return None, "window_baseline_stale"
    before = baseline["values"]["probability"]
    after = observation["values"]["probability"]
    delta_pp = (Decimal(after) - Decimal(before)) * 100
    if abs(delta_pp) < Decimal(rule["min_change_pp"]):
        return None, "below_threshold"
    return {
        **context,
        "mode": "window",
        "baseline_observation_id": baseline["observation_id"],
        "window_start": utc(window_start),
        "window_end": utc(now),
        "baseline_observed_at": baseline["observed_at"],
        "current_observed_at": observation["observed_at"],
        "baseline_sample_at": baseline["values"]["sample_at"],
        "current_sample_at": observation["values"]["sample_at"],
        "before": before,
        "after": after,
        "delta_pp": decimal_text(delta_pp),
    }, "matched"
