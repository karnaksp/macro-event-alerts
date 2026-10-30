"""Live orchestration checks using public-shaped fixtures and fake Telegram only."""
from __future__ import annotations

import copy
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import sqlite3
import socket

import pytest
import yaml

from macro_event_alerts.app import replay_file
from macro_event_alerts.config import load_config
from macro_event_alerts.domain import sha256, utc, instant
from macro_event_alerts.live import LiveWorker, deliver_outbox
from macro_event_alerts.storage import MIGRATION_1, Store
from macro_event_alerts.telegram import SendResult
from macro_event_alerts.validation import ContractError

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def synthetic_credentials(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:offline_test_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1000000000123")
    monkeypatch.setenv("TELEGRAM_MESSAGE_THREAD_ID", "")
    def reject_network(*args, **kwargs):
        raise AssertionError("live integration test attempted a real socket")
    monkeypatch.setattr(socket, "socket", reject_network)


class FakeSender:
    def __init__(self, results=None, checks=None):
        self.messages = []
        self.parse_modes = []
        self.results = list(results or [])
        self.checks = list(checks or [{"ok": True}])
        self.check_count = 0

    def check(self):
        self.check_count += 1
        result = self.checks[0]
        if len(self.checks) > 1:
            self.checks.pop(0)
        return result

    def send(self, text, *, parse_mode=None):
        self.messages.append(text)
        self.parse_modes.append(parse_mode)
        if self.results:
            return self.results.pop(0)
        return SendResult("delivered", message_id=str(100 + len(self.messages)))


def config():
    value = yaml.safe_load((ROOT / "config.replay.yaml").read_text())
    value["telegram"]["dry_run"] = False
    value["providers"] = {"xoomar": {
        "adapter": "xoomar", "enabled": True, "experimental": False,
        "poll_interval_seconds": 60, "daily_request_budget": 100, "options": {},
    }}
    value["rules"] = [copy.deepcopy(value["rules"][0])]
    value["rules"][0].update(provider_ids=["xoomar"], event_types=["macro.release"], cooldown_seconds=0)
    return value


def envelope(at, *, actual=None, revision="1"):
    example = next(json.loads(line)["observation"] for line in (ROOT / "examples/replay/demo.jsonl").read_text().splitlines()
                   if json.loads(line)["observation"]["subject"]["kind"] == "macro")
    observation = copy.deepcopy(example)
    observation["observation_id"] = str(uuid4())
    observation["observed_at"] = utc(at)
    observation["source"].update(provider_id="xoomar", source_updated_at=utc(at), source_revision=revision)
    observation["values"].update(
        actual=actual, forecast="3.0", previous="2.9",
        publication_status="published" if actual is not None else "scheduled",
        published_at=utc(at) if actual is not None else None,
    )
    raw = {"synthetic": True, "source": observation["source"], "values": observation["values"]}
    observation["raw_sha256"] = sha256(raw)
    return {"raw": raw, "observation": observation}


class Calendar:
    def __init__(self, *, fail_at=None):
        self.calls = 0
        self.fail_at = fail_at

    def __call__(self, provider_id, settings, *, now, checkpoint=None):
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("synthetic provider failure")
        return SimpleNamespace(
            envelopes=[envelope(now, actual=None if self.calls == 1 else "3.1", revision=str(self.calls))],
            checkpoint={"cursor": self.calls}, request_count=1,
        )


def queue_from_replay(tmp_path, *, count=1):
    state = tmp_path / "state.db"
    replay_file(ROOT / "config.replay.yaml", ROOT / "examples/replay/demo.jsonl",
                state_path=state, drain=False, output=io.StringIO())
    with Store(state) as store:
        ids = [row[0] for row in store.conn.execute("SELECT outbox_id FROM outbox ORDER BY created_at,outbox_id LIMIT ?", (count,))]
        store.conn.execute("UPDATE outbox SET status='dry_run'")
        for outbox_id in ids:
            store.conn.execute("UPDATE outbox SET delivery_mode='telegram',status='pending' WHERE outbox_id=?", (outbox_id,))
    return state, ids


def test_live_release_reaches_sender_once_and_restart_keeps_identity(tmp_path):
    state = tmp_path / "state.db"
    source, sender = Calendar(), FakeSender()
    with Store(state) as store:
        worker = LiveWorker(config(), store, sender=sender, clock=lambda: T0, fetcher=source)
        worker.tick(T0)
        assert store.stats()["events"] == 0
        worker.tick(T0 + timedelta(seconds=61))
        assert len(sender.messages) == 1
        assert "3,1%" in sender.messages[0]
        assert sender.parse_modes == ["HTML"]
        assert "СИНТЕТИЧЕСКИЙ REPLAY" not in sender.messages[0]
        saved = store.conn.execute("SELECT event_id,message_id,status FROM outbox").fetchone()
        assert saved["status"] == "delivered"
        saved_identity = tuple(saved)
    with Store(state) as store:
        worker = LiveWorker(config(), store, sender=sender, clock=lambda: T0, fetcher=source)
        worker.tick(T0 + timedelta(seconds=62))
        assert tuple(store.conn.execute("SELECT event_id,message_id,status FROM outbox").fetchone()) == saved_identity
        assert len(sender.messages) == 1
        assert source.calls == 2


def test_old_persisted_message_stays_plain_text(tmp_path):
    state, ids = queue_from_replay(tmp_path)
    sender = FakeSender()
    with Store(state) as store:
        store.conn.execute("UPDATE outbox SET evaluation_json='{}',message_text=? WHERE outbox_id=?",
                           ("Saved <value> & source", ids[0]))
        deliver_outbox(store, sender, now=T0)
    assert sender.messages == ["Saved <value> & source"]
    assert sender.parse_modes == [None]


def test_429_pauses_all_jobs_and_persists_retry(tmp_path):
    state, ids = queue_from_replay(tmp_path, count=2)
    sender = FakeSender([SendResult("retry", error_code="telegram_429", retry_after_seconds=120)])
    with Store(state) as store:
        deliver_outbox(store, sender, now=T0, limit=1)
        assert len(sender.messages) == 1
        assert store.conn.execute("SELECT status FROM outbox WHERE outbox_id=?", (ids[0],)).fetchone()[0] == "retry"
        deliver_outbox(store, sender, now=T0 + timedelta(seconds=60), limit=1)
        assert len(sender.messages) == 1  # The second pending job also respects Retry-After.
    with Store(state) as store:
        deliver_outbox(store, sender, now=T0 + timedelta(seconds=121), limit=1)
        assert len(sender.messages) == 2
        assert store.stats()["delivered"] == 1
        deliver_outbox(store, sender, now=T0 + timedelta(seconds=124), limit=1)
        assert len(sender.messages) == 3
        assert store.stats()["delivered"] == 2


def test_ambiguous_timeout_is_retried_with_same_event_id(tmp_path):
    state, ids = queue_from_replay(tmp_path)
    sender = FakeSender([SendResult("ambiguous", error_code="transport_uncertain")])
    with Store(state) as store:
        deliver_outbox(store, sender, now=T0)
        row = store.conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (ids[0],)).fetchone()
        assert row["status"] == "ambiguous"
        assert row["attempts"] == 1
        event_id = row["event_id"]
        retry_at = instant(row["next_attempt_at"])
    with Store(state) as store:
        deliver_outbox(store, sender, now=retry_at + timedelta(seconds=1))
        row = store.conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (ids[0],)).fetchone()
        assert row["status"] == "delivered"
        assert row["event_id"] == event_id
        assert row["attempts"] == 2
        assert len(sender.messages) == 2  # A possible external duplicate is intentional at-least-once behavior.


def test_attempt_limit_moves_retry_to_dead_letter(tmp_path):
    state, ids = queue_from_replay(tmp_path)
    sender = FakeSender([SendResult("retry", error_code="telegram_503")])
    with Store(state) as store:
        store.conn.execute("UPDATE outbox SET attempts=9 WHERE outbox_id=?", (ids[0],))
        deliver_outbox(store, sender, now=T0)
        row = store.conn.execute("SELECT status,attempts FROM outbox WHERE outbox_id=?", (ids[0],)).fetchone()
        assert tuple(row) == ("dead_letter", 10)


def test_sending_job_is_recovered_after_crash(tmp_path):
    state, ids = queue_from_replay(tmp_path)
    with Store(state) as store:
        store.conn.execute("UPDATE outbox SET status='sending',attempts=1,sending_started_at=? WHERE outbox_id=?", (utc(T0), ids[0]))
    sender = FakeSender()
    empty = lambda *args, **kwargs: SimpleNamespace(envelopes=[], checkpoint=None, request_count=1)
    with Store(state) as store:
        worker = LiveWorker(config(), store, sender=sender, clock=lambda: T0, fetcher=empty)
        worker.tick(T0 + timedelta(seconds=60))
        row = store.conn.execute("SELECT status,attempts FROM outbox WHERE outbox_id=?", (ids[0],)).fetchone()
        assert row["status"] in {"ambiguous", "delivered"}
        assert row["attempts"] >= 1
        assert store.stats()["events"] == 7


def test_provider_failure_preserves_baseline_and_checkpoint(tmp_path):
    source, sender = Calendar(fail_at=2), FakeSender()
    with Store(tmp_path / "state.db") as store:
        worker = LiveWorker(config(), store, sender=sender, clock=lambda: T0, fetcher=source)
        worker.tick(T0)
        before = dict(store.conn.execute("SELECT * FROM provider_state WHERE provider_id='xoomar'").fetchone())
        worker.tick(T0 + timedelta(seconds=61))
        after = dict(store.conn.execute("SELECT * FROM provider_state WHERE provider_id='xoomar'").fetchone())
        assert after["checkpoint_json"] == before["checkpoint_json"]
        assert after["last_success_at"] == before["last_success_at"]
        assert after["requests_today"] == 2
        assert after["error"] is not None
        assert store.stats()["observations"] == 1
        assert store.stats()["events"] == 0


def test_daily_budget_survives_restart_and_resets_next_utc_day(tmp_path):
    state = tmp_path / "state.db"
    settings = config()
    settings["providers"]["xoomar"]["daily_request_budget"] = 1
    source, sender = Calendar(), FakeSender()
    with Store(state) as store:
        worker = LiveWorker(settings, store, sender=sender, clock=lambda: T0, fetcher=source)
        worker.tick(T0)
        worker.tick(T0 + timedelta(seconds=61))
        assert source.calls == 1
    with Store(state) as store:
        worker = LiveWorker(settings, store, sender=sender, clock=lambda: T0, fetcher=source)
        worker.tick(T0 + timedelta(minutes=5))
        assert source.calls == 1
        worker.tick(T0 + timedelta(days=1, seconds=1))
        assert source.calls == 2


def test_missing_chat_access_does_not_stop_collection_and_can_recover(tmp_path):
    source = Calendar()
    sender = FakeSender(checks=[{"ok": False, "error_code": "telegram_400"}, {"ok": True}])
    with Store(tmp_path / "state.db") as store:
        worker = LiveWorker(config(), store, sender=sender, clock=lambda: T0, fetcher=source)
        worker.tick(T0)
        assert source.calls == 1
        assert sender.messages == []
        worker.tick(T0 + timedelta(seconds=61))
        assert source.calls == 2
        assert store.stats()["events"] == 1
        assert store.stats()["delivered"] == 1
        assert len(sender.messages) == 1


def test_provider_checkpoint_and_observation_roll_back_together(tmp_path):
    source, sender = Calendar(), FakeSender()
    with Store(tmp_path / "state.db") as store:
        worker = LiveWorker(config(), store, sender=sender, clock=lambda: T0, fetcher=source)
        worker.tick(T0)
        checkpoint = store.conn.execute("SELECT checkpoint_json FROM provider_state WHERE provider_id='xoomar'").fetchone()[0]
        store.conn.execute("CREATE TEMP TRIGGER reject_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'injected storage failure'); END")
        try:
            worker.tick(T0 + timedelta(seconds=61))
        except sqlite3.DatabaseError:
            pass
        assert store.conn.execute("SELECT checkpoint_json FROM provider_state WHERE provider_id='xoomar'").fetchone()[0] == checkpoint
        assert store.stats()["observations"] == 1
        assert store.stats()["events"] == 0
        assert sender.messages == []


def test_v1_database_migration_preserves_queued_dry_run_identity(tmp_path):
    state = tmp_path / "state.db"
    with sqlite3.connect(state) as conn:
        conn.executescript(MIGRATION_1)
        conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", ("synthetic-event", "macro.release", "subject", 1, utc(T0), "{}"))
        conn.execute("INSERT INTO subscriptions VALUES(?,?,?,?,?)", ("synthetic-sub", "rule", "version", "dry-run", utc(T0)))
        conn.execute("INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?)", ("synthetic-job", "synthetic-event", "synthetic-sub", "dry-run", "pending", "{}", "synthetic message", utc(T0)))
    with Store(state) as store:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 2
        row = store.conn.execute("SELECT event_id,outbox_id,status,delivery_mode FROM outbox").fetchone()
        assert tuple(row) == ("synthetic-event", "synthetic-job", "pending", "dry_run")
        assert store.stats()["events"] == 1


def test_unknown_paid_adapter_cannot_be_enabled_implicitly(tmp_path):
    settings = config()
    settings["providers"]["xoomar"]["adapter"] = "unconfigured_paid_vendor"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(settings))
    with pytest.raises(ContractError):
        load_config(path)


def test_queued_foreign_destination_is_not_rerouted_to_current_chat(tmp_path):
    state, ids = queue_from_replay(tmp_path, count=2)
    active = "telegram:-1000000000123:"
    foreign = "telegram:-1000000000456:"
    sender = FakeSender()
    sender.destination = active
    with Store(state) as store:
        store.conn.execute("UPDATE outbox SET destination=? WHERE outbox_id=?", (foreign, ids[0]))
        store.conn.execute("UPDATE outbox SET destination=? WHERE outbox_id=?", (active, ids[1]))
        assert deliver_outbox(store, sender, now=T0, limit=2) == 1
        assert len(sender.messages) == 1
        old = store.conn.execute("SELECT destination,status,attempts FROM outbox WHERE outbox_id=?", (ids[0],)).fetchone()
        assert tuple(old) == (foreign, "pending", 0)
        assert store.conn.execute("SELECT status FROM outbox WHERE outbox_id=?", (ids[1],)).fetchone()[0] == "delivered"
        assert deliver_outbox(store, sender, now=T0 + timedelta(seconds=4)) == 0
        assert len(sender.messages) == 1


def test_crashed_sending_at_attempt_limit_does_not_send_eleventh_time(tmp_path):
    state, ids = queue_from_replay(tmp_path)
    sender = FakeSender()
    with Store(state) as store:
        store.conn.execute(
            "UPDATE outbox SET status='sending',attempts=10,sending_started_at=? WHERE outbox_id=?",
            (utc(T0), ids[0]),
        )
    empty = lambda *args, **kwargs: SimpleNamespace(envelopes=[], checkpoint=None, request_count=1)
    with Store(state) as store:
        worker = LiveWorker(config(), store, sender=sender, clock=lambda: T0, fetcher=empty)
        worker.tick(T0 + timedelta(seconds=60))
        row = store.conn.execute("SELECT status,attempts,next_attempt_at FROM outbox WHERE outbox_id=?", (ids[0],)).fetchone()
        assert tuple(row) == ("dead_letter", 10, None)
        assert sender.messages == []
