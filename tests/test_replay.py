"""Offline P1 integration checks against persisted state and rendered dry-run jobs."""
from __future__ import annotations

import copy
import io
import json
import socket
from collections import Counter
from pathlib import Path
from uuid import UUID

import pytest

from macro_event_alerts.app import replay_file
from macro_event_alerts.storage import Store

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.replay.yaml"
DEMO = ROOT / "examples/replay/demo.jsonl"


def records() -> list[dict]:
    return [json.loads(line) for line in DEMO.read_text().splitlines()]


def fixture_file(tmp_path: Path, items: list[dict]) -> Path:
    path = tmp_path / "input.jsonl"
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items))
    return path


def persisted(state: Path) -> tuple[dict, list[dict], list[dict]]:
    with Store(state) as store:
        counts = store.stats()
        events = [json.loads(row[0]) for row in store.conn.execute(
            "SELECT body_json FROM events ORDER BY observed_at, event_id"
        )]
        jobs = [dict(row) for row in store.conn.execute("SELECT * FROM outbox ORDER BY created_at, outbox_id")]
    return counts, events, jobs


def test_demo_is_durable_and_replay_does_not_repeat_jobs(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    first = replay_file(CONFIG, DEMO, state_path=state, output=io.StringIO())
    counts, events, jobs = persisted(state)
    assert first["processed"] == 12
    assert counts["observations"] == 12
    assert counts["events"] == 7
    assert counts["outbox"] == counts["dry_run"] == 6
    assert counts["pending"] == counts["quarantine"] == 0
    assert Counter(event["event_type"] for event in events) == {
        "macro.forecast_changed": 2,
        "macro.release": 2,
        "macro.revised": 1,
        "scenario.probability_changed": 2,
    }
    ids = [event["event_id"] for event in events]
    assert len(ids) == len(set(ids))
    for event_id in ids:
        UUID(event_id)
    second_output = io.StringIO()
    second = replay_file(CONFIG, DEMO, state_path=state, output=second_output)
    assert second["processed"] == 0
    next_counts, next_events, next_jobs = persisted(state)
    assert next_counts == counts
    assert next_events == events
    assert next_jobs == jobs


def test_cold_start_and_first_forecast_only_initialize_baselines(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    replay_file(CONFIG, DEMO, state_path=state, stop_after=3, output=io.StringIO())
    counts, events, jobs = persisted(state)
    assert counts["observations"] == 3
    assert events == jobs == []
    with Store(state) as store:
        assert store.conn.execute("SELECT line_number FROM provider_checkpoints").fetchone()[0] == 3
    replay_file(CONFIG, DEMO, state_path=state, output=io.StringIO())
    assert persisted(state)[0]["events"] == 7


def test_a_to_b_to_a_are_distinct_forecast_events(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    replay_file(CONFIG, DEMO, state_path=state, output=io.StringIO())
    _, events, _ = persisted(state)
    forecasts = [event for event in events if event["event_type"] == "macro.forecast_changed"]
    assert [(event["payload"]["before"], event["payload"]["after"]) for event in forecasts] == [
        ("2.9", "3.0"), ("3.0", "2.9"),
    ]
    assert len({event["event_id"] for event in forecasts}) == 2
    assert forecasts[0]["state_version"] < forecasts[1]["state_version"]
    assert forecasts[0]["subject_key"] == forecasts[1]["subject_key"]
    assert all(event["observed_at"] < "2026-10-14T12:30:00Z" for event in forecasts)
    releases = [event for event in events if event["event_type"] == "macro.release"]
    assert {event["subject"]["reference_period"] for event in releases} == {"2026-09", "2026-10"}
    october = next(event for event in releases if event["subject"]["reference_period"] == "2026-10")
    assert october["payload"]["actual"] == october["payload"]["previous"] == "3.2"


def test_window_context_preserves_twenty_pp_not_last_eleven_pp(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    replay_file(CONFIG, DEMO, state_path=state, output=io.StringIO())
    _, events, jobs = persisted(state)
    scenario_events = {event["event_id"]: event for event in events if event["event_type"] == "scenario.probability_changed"}
    scenario_jobs = [job for job in jobs if job["event_id"] in scenario_events]
    assert len(scenario_jobs) == 1
    job = scenario_jobs[0]
    event = scenario_events[job["event_id"]]
    assert event["payload"]["delta_pp"] == "11"
    context = json.loads(job["evaluation_json"])
    assert context["before"] == "0.55"
    assert context["after"] == "0.75"
    assert context["delta_pp"] == "20"
    assert context["baseline_observation_id"] == records()[1]["observation"]["observation_id"]
    assert context["current_observation_id"] == records()[6]["observation"]["observation_id"]
    assert "20" in job["message_text"]


@pytest.mark.parametrize("crash_at", [1, 4])
def test_crash_rolls_back_observation_event_outbox_and_checkpoint(tmp_path: Path, crash_at: int) -> None:
    state = tmp_path / "state.db"
    calls = 0

    def crash_before_commit() -> None:
        nonlocal calls
        calls += 1
        if calls == crash_at:
            raise RuntimeError("injected crash before commit")

    with pytest.raises(RuntimeError, match="injected crash"):
        replay_file(CONFIG, DEMO, state_path=state, drain=False, before_commit=crash_before_commit)
    counts, events, jobs = persisted(state)
    assert counts["observations"] == crash_at - 1
    assert events == jobs == []
    with Store(state) as store:
        row = store.conn.execute("SELECT MAX(line_number) FROM provider_checkpoints").fetchone()
        assert (row[0] or 0) == crash_at - 1
    replay_file(CONFIG, DEMO, state_path=state, output=io.StringIO())
    recovered_counts, recovered_events, _ = persisted(state)
    assert recovered_counts["observations"] == 12
    assert recovered_counts["events"] == len(recovered_events) == 7
    assert recovered_counts["dry_run"] == 6


def test_pending_outbox_is_drained_after_restart_without_new_input(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    replay_file(CONFIG, DEMO, state_path=state, drain=False)
    assert persisted(state)[0]["pending"] == 6
    assert persisted(state)[0]["dry_run"] == 0
    result = replay_file(CONFIG, DEMO, state_path=state, output=io.StringIO())
    assert result["processed"] == 0
    counts = persisted(state)[0]
    assert counts["pending"] == 0
    assert counts["dry_run"] == 6


def test_duplicate_input_observation_does_not_create_second_transition(tmp_path: Path) -> None:
    data = records()
    fixture = fixture_file(tmp_path, data + [copy.deepcopy(data[-1])])
    state = tmp_path / "state.db"
    replay_file(CONFIG, fixture, state_path=state, output=io.StringIO())
    counts, _, _ = persisted(state)
    assert counts["observations"] == 12
    assert counts["events"] == 7
    assert counts["outbox"] == 6


def test_invalid_observation_is_quarantined_without_replacing_baseline(tmp_path: Path) -> None:
    data = records()
    malformed = copy.deepcopy(data[3])
    del malformed["observation"]["subject"]["unit"]
    fixture = fixture_file(tmp_path, [data[0], malformed, data[2]])
    state = tmp_path / "state.db"
    replay_file(CONFIG, fixture, state_path=state, output=io.StringIO())
    counts, events, jobs = persisted(state)
    assert counts["quarantine"] == 1
    assert events == jobs == []
    with Store(state) as store:
        assert store.conn.execute("SELECT MAX(line_number) FROM provider_checkpoints").fetchone()[0] == 3


def test_starting_from_published_history_is_silent(tmp_path: Path) -> None:
    fixture = fixture_file(tmp_path, [records()[7]])
    state = tmp_path / "state.db"
    replay_file(CONFIG, fixture, state_path=state, output=io.StringIO())
    counts, events, jobs = persisted(state)
    assert counts["observations"] == 1
    assert events == jobs == []


def test_dry_run_never_opens_network_even_with_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline replay attempted network access")

    monkeypatch.setattr(socket, "socket", reject_network)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-offline-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1000000000000")  # Synthetic destination.
    state = tmp_path / "state.db"
    replay_file(CONFIG, DEMO, state_path=state, output=io.StringIO())
    assert persisted(state)[0]["dry_run"] == 6
