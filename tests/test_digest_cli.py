"""Manual multi-page digests use fake Telegram and never access real credentials."""
from __future__ import annotations

import json
import copy
from pathlib import Path
import socket
import sqlite3
from uuid import uuid4

import pytest

import macro_event_alerts.cli as cli
from macro_event_alerts.app import _ingest
from macro_event_alerts.config import load_config
from macro_event_alerts.domain import sha256
from macro_event_alerts.storage import Store
from macro_event_alerts.telegram import SendResult
from macro_event_alerts.validation import get_contracts

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("digest CLI test attempted a real socket")
    monkeypatch.setattr(socket, "socket", reject)


def manual_command(monkeypatch, results, *, checked=True):
    calls = []
    responses = iter(results)
    class FakeSender:
        def check(self):
            calls.append(("check",))
            return {"ok": checked}

        def send(self, text, *, parse_mode):
            calls.append(("send", text, parse_mode))
            return next(responses)

    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: {"telegram": {"dry_run": False}})
    monkeypatch.setattr(cli, "telegram_environment", lambda config: ("offline_token", "offline_chat", None))
    monkeypatch.setattr(cli, "TelegramSender", lambda *args: FakeSender())
    monkeypatch.setattr(cli, "snapshot_messages", lambda config: ["<b>Факты</b>", "<b>Календарь</b>"])
    monkeypatch.setattr(cli, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    return calls


def test_manual_digest_sends_every_page_as_html_with_pacing(monkeypatch, capsys):
    calls = manual_command(monkeypatch, [SendResult("delivered", message_id="10"), SendResult("delivered", message_id="11")])
    assert cli.main(["telegram-test"]) == 0
    assert calls == [("check",), ("send", "<b>Факты</b>", "HTML"), ("sleep", 3), ("send", "<b>Календарь</b>", "HTML")]
    output = capsys.readouterr().out
    reports = [json.loads(line) for line in output.splitlines()]
    assert [(item["page"], item["pages"], item["status"], item["message_id"]) for item in reports] == [
        (1, 2, "delivered", "10"), (2, 2, "delivered", "11"),
    ]
    assert all(value not in output for value in ("offline_token", "offline_chat", "<b>"))


def test_manual_digest_stops_after_first_failed_page(monkeypatch, capsys):
    calls = manual_command(monkeypatch, [SendResult("retry", error_code="telegram_429", retry_after_seconds=30)])
    assert cli.main(["telegram-test"]) == 2
    assert calls == [("check",), ("send", "<b>Факты</b>", "HTML")]
    report = json.loads(capsys.readouterr().out)
    assert report == {"page": 1, "pages": 2, "status": "retry", "message_id": None, "error_code": "telegram_429"}


def test_telegram_check_stays_read_only(monkeypatch, capsys):
    calls = manual_command(monkeypatch, [])
    assert cli.main(["telegram-check"]) == 0
    assert calls == [("check",)]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_snapshot_reader_passes_all_saved_subjects_to_renderer(tmp_path, monkeypatch):
    state = tmp_path / "state.db"
    with sqlite3.connect(state) as conn:
        conn.execute("CREATE TABLE observations(observation_id TEXT, body_json TEXT, observed_at TEXT, subject_key TEXT, accepted INTEGER)")
        conn.execute("CREATE TABLE subject_state(observation_id TEXT)")
        for index in range(121):
            identifier = str(index)
            conn.execute("INSERT INTO observations VALUES(?,?,?,?,?)", (identifier, json.dumps({"marker": index}), "2026-10-30T11:00:00Z", identifier, 1))
            conn.execute("INSERT INTO subject_state VALUES(?)", (identifier,))
    captured = {}
    def render(items, *, dry_run):
        captured.update(items=items, dry_run=dry_run)
        return ["digest"]
    monkeypatch.setattr(cli, "render_snapshot_messages", render)
    assert cli.snapshot_messages({"service": {"state_path": str(state)}, "telegram": {"dry_run": False}}) == ["digest"]
    assert {item["marker"] for item in captured["items"]} == set(range(121))
    assert captured["dry_run"] is False


def test_digest_includes_latest_backfill_without_changing_live_baseline(tmp_path, monkeypatch):
    state = tmp_path / "state.db"
    config = load_config(ROOT / "config.replay.yaml", state_path=state)
    template = next(json.loads(line)["observation"] for line in (ROOT / "examples/replay/demo.jsonl").read_text().splitlines()
                    if json.loads(line)["observation"]["subject"]["kind"] == "macro")
    contracts = get_contracts()

    def envelope(*, historical, at, actual):
        item = copy.deepcopy(template)
        item.update(observation_id=str(uuid4()), observed_at=at, is_backfill=historical)
        item["subject"].update(release_key="historical-cpi" if historical else "upcoming-cpi",
                               reference_period="2026-09" if historical else "2026-10")
        item["values"].update(actual=actual, publication_status="published" if actual is not None else "scheduled",
                              published_at="2026-10-14T12:30:00Z" if historical else None,
                              scheduled_at="2026-10-14T12:30:00Z" if historical else "2026-11-10T13:30:00Z")
        raw = {"synthetic": True, "values": item["values"]}
        item["raw_sha256"] = sha256(raw)
        return {"raw": raw, "observation": item}

    with Store(state) as store:
        with store.transaction():
            _ingest(store, config, contracts, envelope(historical=False, at="2026-10-30T10:00:00Z", actual=None))
        baseline = [tuple(row) for row in store.conn.execute("SELECT * FROM subject_state ORDER BY subject_key")]
        for actual in ("3.1", "3.2"):
            # Same observation time exercises rowid as the deterministic tie-breaker.
            with store.transaction():
                _, reason = _ingest(store, config, contracts, envelope(historical=True, at="2026-10-30T11:00:00Z", actual=actual))
            assert reason == "backfill"
        assert [tuple(row) for row in store.conn.execute("SELECT * FROM subject_state ORDER BY subject_key")] == baseline
        assert store.stats()["events"] == store.stats()["outbox"] == 0
        assert store.conn.execute("SELECT count(*) FROM observations WHERE accepted=0").fetchone()[0] == 2

    seen = []
    renderer = cli.render_snapshot_messages
    def capture(items, *, dry_run):
        seen.extend(items)
        return renderer(items, at="2026-10-30T11:05:00Z", dry_run=dry_run)
    monkeypatch.setattr(cli, "render_snapshot_messages", capture)
    rendered = "\n".join(cli.snapshot_messages(config))
    assert len(seen) == 2
    assert [item["values"]["actual"] for item in seen if item["is_backfill"]] == ["3.2"]
    assert "3,2%" in rendered and "3,1%" not in rendered
