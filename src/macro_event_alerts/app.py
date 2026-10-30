"""Finite JSONL replay with a logical clock and durable dry-run outbox."""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import sys
from typing import Callable, TextIO
from uuid import uuid4

from .config import load_config
from .domain import canonical, instant, sha256, subject_key
from .engine import detect, evaluate_rule
from .render import render
from .storage import Store
from .validation import Contracts, ContractError


def _stale(previous: dict, observation: dict) -> str | None:
    if instant(observation["observed_at"]) < instant(previous["observed_at"]):
        return "older_observation"
    old, new = previous["source"], observation["source"]
    a, b = old["source_revision"], new["source_revision"]
    changed = previous["values"] != observation["values"]
    revision_orders = a is not None and b is not None and a.isdecimal() and b.isdecimal()
    if revision_orders:
        if int(b) < int(a):
            return "older_source_revision"
        if int(a) == int(b) and changed:
            return "conflicting_source_revision"
    a, b = old["source_updated_at"], new["source_updated_at"]
    if not revision_orders and a is not None and b is not None and instant(b) < instant(a):
        return "older_source_timestamp"
    if changed and instant(observation["observed_at"]) <= instant(previous["observed_at"]):
        return "nonmonotonic_observation_change"
    if observation["subject"]["kind"] == "macro":
        if previous["values"]["actual"] is not None and observation["values"]["actual"] is None:
            return "published_actual_removed"
    return None


def _subscription(store: Store, rule: dict, config: dict, at: str) -> dict:
    version = sha256(rule)
    # P1 deliberately does not load .env or private destinations.
    destination = "dry-run:" + config["telegram"]["chat_id_env"] + ":" + (config["telegram"]["message_thread_id_env"] or "")
    identity = sha256({"rule_id": rule["id"], "version": version, "destination": destination})
    store.conn.execute("INSERT OR IGNORE INTO subscriptions VALUES(?,?,?,?,?)",
                       (identity, rule["id"], version, destination, at))
    return dict(store.conn.execute("SELECT * FROM subscriptions WHERE subscription_id=?", (identity,)).fetchone())


def _ingest(store: Store, config: dict, contracts: Contracts, envelope: dict) -> tuple[list[dict], str | None]:
    observation = envelope["observation"]
    contracts.validate(observation, "observation.schema.json")
    raw = envelope["raw"]
    if sha256(raw) != observation["raw_sha256"]:
        raise ContractError("raw_sha256 does not match raw response")
    provider_id = observation["source"]["provider_id"]
    provider = config["providers"].get(provider_id)
    if not provider or not provider["enabled"]:
        raise ContractError("observation provider is disabled or absent")
    existing = store.conn.execute("SELECT body_json FROM observations WHERE observation_id=?", (observation["observation_id"],)).fetchone()
    if existing:
        if canonical(observation) != existing[0]:
            raise ContractError("observation_id conflicts with saved content")
        return [], "duplicate_observation"
    key = subject_key(observation)
    state = store.previous(key)
    previous = json.loads(state["body_json"]) if state else None
    reason = _stale(previous, observation) if previous and not observation["is_backfill"] else None
    if reason:
        raise ContractError(reason)
    accepted = not observation["is_backfill"]
    store.conn.execute("INSERT OR IGNORE INTO raw_responses VALUES(?,?)", (observation["raw_sha256"], canonical(raw)))
    store.conn.execute("INSERT INTO observations VALUES(?,?,?,?,?,?)", (
        observation["observation_id"], key, provider_id, observation["observed_at"], int(accepted), canonical(observation)))
    if not accepted:
        return [], "backfill"
    changed = previous is None or previous["values"] != observation["values"]
    version = (state["state_version"] if state else 0) + int(changed)
    original_id = state["original_release_event_id"] if state else None
    events = detect(previous, observation, state_version=version, original_release_event_id=original_id) if changed else []
    for event in events:
        try:
            contracts.validate(event, "event.schema.json")
        except ContractError as exc:
            raise RuntimeError("detector generated an invalid event; transaction rolled back") from exc
        store.conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (
            event["event_id"], event["event_type"], key, version, event["observed_at"], canonical(event)))
        if event["event_type"] == "macro.release":
            original_id = event["event_id"]
    store.conn.execute("""INSERT INTO subject_state VALUES(?,?,?,?) ON CONFLICT(subject_key)
        DO UPDATE SET observation_id=excluded.observation_id,state_version=excluded.state_version,
        original_release_event_id=excluded.original_release_event_id""", (key, observation["observation_id"], version, original_id))
    history = store.history(key)
    diagnostics = []
    rule_events = events
    if observation["subject"]["kind"] == "scenario" and not events:
        # A fresh unchanged quote can complete a comparison window after the price moved.
        latest = store.conn.execute(
            "SELECT body_json FROM events WHERE subject_key=? AND event_type=? ORDER BY rowid DESC LIMIT 1",
            (key, "scenario.probability_changed"),
        ).fetchone()
        if latest:
            rule_events = [json.loads(latest[0])]
    for rule in config["rules"]:
        if not rule["enabled"]:
            continue
        subscription = _subscription(store, rule, config, observation["observed_at"])
        last = store.conn.execute("SELECT max(created_at) FROM outbox WHERE subscription_id=?", (subscription["subscription_id"],)).fetchone()[0]
        for event in rule_events:
            context, why = evaluate_rule(rule, event, observation, history, now=instant(observation["observed_at"]),
                                         last_alert_at=last, activated_at=subscription["activated_at"])
            if context is None:
                diagnostics.append({"rule_id": rule["id"], "event_id": event["event_id"], "reason": why})
                continue
            context["rule_version"] = subscription["rule_version"]
            inserted = store.conn.execute("INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?,?,?,?)", (
                str(uuid4()), event["event_id"], subscription["subscription_id"], subscription["destination"],
                "pending", canonical(context), render(event, context), observation["observed_at"]))
            if inserted.rowcount:
                last = observation["observed_at"]
    return diagnostics, None


def drain_outbox(store: Store, output: TextIO) -> int:
    rows = store.conn.execute("SELECT outbox_id,event_id,message_text FROM outbox WHERE status='pending' ORDER BY rowid").fetchall()
    for row in rows:
        # Stdout can duplicate if interrupted after writing but before marking the row.
        output.write(json.dumps({"type": "dry_run", "event_id": row["event_id"], "message": row["message_text"]}, ensure_ascii=False) + "\n")
        output.flush()
        with store.transaction():
            store.conn.execute("UPDATE outbox SET status='dry_run' WHERE outbox_id=?", (row["outbox_id"],))
    return len(rows)


def replay_file(config_path: Path | str, fixture_path: Path | str, *, state_path: Path | str | None = None,
                stop_after: int | None = None, drain: bool = True, output: TextIO | None = None,
                before_commit: Callable[[], None] | None = None, _store: Store | None = None) -> dict:
    config = load_config(config_path, state_path=state_path)
    contracts = Contracts()
    content = Path(fixture_path).read_bytes()
    fixture_key = hashlib.sha256(content).hexdigest()
    stream = output if output is not None else sys.stdout
    processed = skipped = quarantined = 0
    diagnostics = []
    with (nullcontext(_store) if _store is not None else Store(config["service"]["state_path"])) as store:
        for number, line in enumerate(content.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                envelope = json.loads(line)
                observation = envelope.get("observation", {})
                provider = observation.get("source", {}).get("provider_id", "_invalid")
                if not isinstance(provider, str):
                    provider = "_invalid"
            except (json.JSONDecodeError, AttributeError):
                envelope = {"invalid_line": number}
                observation, provider = {}, "_invalid"
            if store.checkpoint(provider, fixture_key) >= number:
                skipped += 1
                continue
            if stop_after is not None and processed >= stop_after:
                break
            with store.transaction():
                # Savepoint ensures malformed data cannot leave raw/observation/event fragments.
                store.conn.execute("SAVEPOINT ingest")
                try:
                    if "observation" not in envelope or "raw" not in envelope:
                        raise ContractError("fixture line requires raw and observation")
                    info, why = _ingest(store, config, contracts, envelope)
                    diagnostics.extend(info)
                    if why:
                        diagnostics.append({"line": number, "reason": why})
                    store.conn.execute("RELEASE ingest")
                except (ContractError, KeyError, TypeError, ValueError) as exc:
                    store.conn.execute("ROLLBACK TO ingest")
                    store.conn.execute("RELEASE ingest")
                    reason = str(exc) if isinstance(exc, ContractError) else "invalid_observation_structure"
                    store.conn.execute("INSERT INTO quarantine(reason,body_json,observed_at) VALUES(?,?,?)", (
                        reason, canonical(envelope), observation.get("observed_at") if isinstance(observation, dict) else None))
                    quarantined += 1
                    diagnostics.append({"line": number, "reason": reason})
                store.advance(provider, fixture_key, number)
                if before_commit is not None:
                    before_commit()
            processed += 1
        if drain:
            drain_outbox(store, stream)
        return {**store.status(), "processed": processed, "skipped": skipped, "quarantined": quarantined, "diagnostics": diagnostics}
