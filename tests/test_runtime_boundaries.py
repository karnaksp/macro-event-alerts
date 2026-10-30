"""Regressions for real P1 window, ordering and baseline-poisoning failures."""
from __future__ import annotations

import copy
import io
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from macro_event_alerts.app import replay_file
from macro_event_alerts.domain import sha256, subject_key
from macro_event_alerts.storage import Store
from macro_event_alerts.validation import ContractError, Contracts

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.replay.yaml"
DEMO = ROOT / "examples/replay/demo.jsonl"


def observation(kind: str, at: str) -> dict:
    records = [json.loads(line)["observation"] for line in DEMO.read_text().splitlines()]
    result = copy.deepcopy(next(item for item in records if item["subject"]["kind"] == kind))
    result["observation_id"] = str(uuid4())
    result["observed_at"] = at
    result["source"]["source_updated_at"] = at
    return result


def fixture_file(tmp_path: Path, observations: list[dict]) -> Path:
    envelopes = []
    for item in observations:
        raw = {"synthetic": True, "values": copy.deepcopy(item["values"]), "source": copy.deepcopy(item["source"])}
        item["raw_sha256"] = sha256(raw)
        envelopes.append({"raw": raw, "observation": item})
    path = tmp_path / "regression.jsonl"
    path.write_text("".join(json.dumps(item) + "\n" for item in envelopes))
    return path


def test_window_alert_can_mature_while_current_price_stays_unchanged(tmp_path: Path):
    inputs = []
    for revision, (price, at) in enumerate([
        ("0.55", "2026-10-14T10:00:00Z"),
        ("0.75", "2026-10-14T10:30:00Z"),
        ("0.75", "2026-10-14T11:00:00Z"),
        ("0.75", "2026-10-14T11:05:00Z"),
    ], 1):
        item = observation("scenario", at)
        item["source"]["source_revision"] = str(revision)
        item["values"].update(
            probability=price,
            sample_at=at,
            best_bid=str(Decimal(price) - Decimal("0.01")),
            best_ask=str(Decimal(price) + Decimal("0.01")),
        )
        inputs.append(item)
    state = tmp_path / "state.db"
    result = replay_file(CONFIG, fixture_file(tmp_path, inputs), state_path=state, output=io.StringIO())
    assert result["counts"]["events"] == 1  # No invented zero-delta change event.
    assert result["counts"]["outbox"] == 1
    assert result["counts"]["quarantine"] == 0
    with Store(state) as store:
        job = store.conn.execute("SELECT evaluation_json FROM outbox").fetchone()
        context = json.loads(job[0])
    assert context["before"] == "0.55"
    assert context["after"] == "0.75"
    assert context["delta_pp"] == "20"
    assert context["window_end"] == "2026-10-14T11:00:00Z"
    assert context["current_observation_id"] == inputs[2]["observation_id"]


@pytest.mark.parametrize("field", ["source_updated_at", "published_at"])
def test_future_source_or_publication_time_fails_before_ingestion(field: str):
    item = observation("macro", "2026-10-14T13:00:00Z")
    if field == "source_updated_at":
        item["source"][field] = "2099-01-01T00:00:00Z"
    else:
        item["values"].update(publication_status="published", actual="3.1", published_at="2099-01-01T00:00:00Z")
    with pytest.raises(ContractError, match=field):
        Contracts().validate(item, "observation.schema.json")


def test_future_timestamp_cannot_poison_baseline_and_reject_next_valid_sample(tmp_path: Path):
    future = observation("macro", "2026-10-14T10:00:00Z")
    future["source"].update(source_updated_at="2099-01-01T00:00:00Z", source_revision=None)
    future["values"]["forecast"] = "2.9"
    valid = observation("macro", "2026-10-14T10:01:00Z")
    valid["source"]["source_revision"] = None
    valid["values"]["forecast"] = "3.0"
    state = tmp_path / "state.db"
    result = replay_file(CONFIG, fixture_file(tmp_path, [future, valid]), state_path=state, output=io.StringIO())
    assert result["counts"]["observations"] == 1
    assert result["counts"]["quarantine"] == 1
    assert result["counts"]["events"] == 0
    with Store(state) as store:
        accepted = json.loads(store.previous(subject_key(valid))["body_json"])
        assert accepted["observation_id"] == valid["observation_id"]
        assert store.conn.execute("SELECT observation_id FROM observations").fetchone()[0] != future["observation_id"]


@pytest.mark.parametrize("publication_status", ["scheduled", "cancelled"])
def test_unpublished_status_cannot_carry_an_actual(publication_status: str):
    item = observation("macro", "2026-10-14T10:00:00Z")
    item["values"].update(publication_status=publication_status, actual="3.1")
    with pytest.raises(ContractError, match="observation.schema.json"):
        Contracts().validate(item, "observation.schema.json")
    item["values"]["actual"] = None
    Contracts().validate(item, "observation.schema.json")


def test_numeric_revision_precedes_source_clock_and_lower_revision_is_rejected(tmp_path: Path):
    first = observation("macro", "2026-10-14T10:00:00Z")
    first["source"]["source_revision"] = "1"
    first["values"]["forecast"] = "2.9"
    newer_revision = observation("macro", "2026-10-14T10:01:00Z")
    newer_revision["source"].update(source_revision="2", source_updated_at="2026-10-14T09:59:00Z")
    newer_revision["values"]["forecast"] = "3.0"
    older_revision = observation("macro", "2026-10-14T10:02:00Z")
    older_revision["source"]["source_revision"] = "1"
    older_revision["values"]["forecast"] = "3.1"
    state = tmp_path / "state.db"
    result = replay_file(CONFIG, fixture_file(tmp_path, [first, newer_revision, older_revision]), state_path=state, output=io.StringIO())
    assert result["counts"]["observations"] == 2
    assert result["counts"]["events"] == 1
    assert result["counts"]["quarantine"] == 1
    assert any(item["reason"] == "older_source_revision" for item in result["diagnostics"])
    with Store(state) as store:
        saved = json.loads(store.previous(subject_key(newer_revision))["body_json"])
    assert saved["source"]["source_revision"] == "2"
    assert saved["values"]["forecast"] == "3.0"


def test_second_worker_cannot_open_state_until_first_releases_lock(tmp_path: Path):
    state = tmp_path / "state.db"
    with Store(state):
        with pytest.raises(RuntimeError, match="another replay worker"):
            Store(state)
    with Store(state) as reopened:
        assert reopened.stats()["events"] == 0


def test_historical_backfill_is_stored_without_rewinding_live_forecast(tmp_path: Path):
    first = observation("macro", "2026-10-14T10:00:00Z")
    first["values"]["forecast"] = "2.9"
    historical = observation("macro", "2026-10-14T09:59:00Z")
    historical["is_backfill"] = True
    historical["values"]["forecast"] = "3.5"
    latest = observation("macro", "2026-10-14T10:01:00Z")
    latest["values"]["forecast"] = "3.0"
    for item in (first, historical, latest):
        item["source"]["source_revision"] = None
    state = tmp_path / "state.db"
    result = replay_file(CONFIG, fixture_file(tmp_path, [first, historical, latest]), state_path=state, output=io.StringIO())
    assert result["counts"]["observations"] == 3
    assert result["counts"]["quarantine"] == 0
    assert result["counts"]["events"] == 1
    with Store(state) as store:
        event = json.loads(store.conn.execute("SELECT body_json FROM events").fetchone()[0])
        assert event["payload"]["before"] == "2.9"
        assert event["payload"]["after"] == "3.0"
        assert store.conn.execute("SELECT count(*) FROM observations WHERE accepted=0").fetchone()[0] == 1


def test_invalid_generated_event_aborts_instead_of_advancing_checkpoint(tmp_path: Path, monkeypatch):
    import macro_event_alerts.app as app
    monkeypatch.setattr(app, "detect", lambda *args, **kwargs: [{}])
    state = tmp_path / "state.db"
    with pytest.raises(RuntimeError, match="invalid event"):
        replay_file(CONFIG, DEMO, state_path=state, output=io.StringIO())
    with Store(state) as store:
        assert store.stats()["observations"] == 0
        assert store.stats()["quarantine"] == 0
        assert store.conn.execute("SELECT count(*) FROM provider_checkpoints").fetchone()[0] == 0
