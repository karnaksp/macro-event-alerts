"""SQLite state and one-writer process lock. Checkpoints commit with events/outbox."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3

MIGRATION_1 = """
CREATE TABLE raw_responses (raw_sha256 TEXT PRIMARY KEY, body_json TEXT NOT NULL);
CREATE TABLE observations (
    observation_id TEXT PRIMARY KEY, subject_key TEXT NOT NULL, provider_id TEXT NOT NULL,
    observed_at TEXT NOT NULL, accepted INTEGER NOT NULL CHECK(accepted IN (0,1)), body_json TEXT NOT NULL
);
CREATE INDEX observation_history ON observations(subject_key, observed_at);
CREATE TABLE subject_state (
    subject_key TEXT PRIMARY KEY, observation_id TEXT NOT NULL REFERENCES observations(observation_id),
    state_version INTEGER NOT NULL, original_release_event_id TEXT
);
CREATE TABLE events (
    event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, subject_key TEXT NOT NULL,
    state_version INTEGER NOT NULL, observed_at TEXT NOT NULL, body_json TEXT NOT NULL,
    UNIQUE(subject_key,state_version,event_type)
);
CREATE TABLE subscriptions (
    subscription_id TEXT PRIMARY KEY, rule_id TEXT NOT NULL, rule_version TEXT NOT NULL,
    destination TEXT NOT NULL, activated_at TEXT NOT NULL
);
CREATE TABLE outbox (
    outbox_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(event_id),
    subscription_id TEXT NOT NULL REFERENCES subscriptions(subscription_id), destination TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','dry_run')),
    evaluation_json TEXT NOT NULL, message_text TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(event_id,subscription_id,destination)
);
CREATE TABLE provider_checkpoints (
    provider_id TEXT NOT NULL, fixture_key TEXT NOT NULL, line_number INTEGER NOT NULL,
    PRIMARY KEY(provider_id,fixture_key)
);
CREATE TABLE quarantine (
    id INTEGER PRIMARY KEY, reason TEXT NOT NULL, body_json TEXT NOT NULL, observed_at TEXT
);
PRAGMA user_version=1;
"""



MIGRATION_2 = """
ALTER TABLE outbox RENAME TO outbox_v1;
CREATE TABLE outbox (
    outbox_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(event_id),
    subscription_id TEXT NOT NULL REFERENCES subscriptions(subscription_id), destination TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','dry_run','sending','retry','ambiguous','delivered','dead_letter')),
    evaluation_json TEXT NOT NULL, message_text TEXT NOT NULL, created_at TEXT NOT NULL,
    delivery_mode TEXT NOT NULL DEFAULT 'dry_run' CHECK(delivery_mode IN ('dry_run','telegram')),
    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, last_error TEXT,
    message_id TEXT, sending_started_at TEXT,
    UNIQUE(event_id,subscription_id,destination)
);
INSERT INTO outbox(outbox_id,event_id,subscription_id,destination,status,evaluation_json,message_text,created_at)
SELECT outbox_id,event_id,subscription_id,destination,status,evaluation_json,message_text,created_at FROM outbox_v1;
DROP TABLE outbox_v1;
CREATE INDEX outbox_due ON outbox(delivery_mode,status,next_attempt_at);
CREATE TABLE provider_state (
    provider_id TEXT PRIMARY KEY, initialized INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT, last_attempt_at TEXT, next_poll_at TEXT,
    error TEXT, request_date TEXT, requests_today INTEGER NOT NULL DEFAULT 0,
    checkpoint_json TEXT
);
CREATE TABLE runtime_state (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
PRAGMA user_version=2;
"""

def inspect_state(path: Path | str) -> dict:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise ValueError("unsupported state schema version")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        return _status(conn)


def _stats(conn: sqlite3.Connection) -> dict:
    counts = {name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
              for name in ("observations", "events", "outbox", "quarantine")}
    for status in ("pending", "dry_run", "sending", "retry", "ambiguous", "delivered", "dead_letter"):
        counts[status] = conn.execute("SELECT count(*) FROM outbox WHERE status=?", (status,)).fetchone()[0]
    return counts


def _status(conn: sqlite3.Connection) -> dict:
    return {
        "counts": _stats(conn),
        "event_ids": [row[0] for row in conn.execute("SELECT event_id FROM events ORDER BY rowid")],
        "providers": [dict(row) for row in conn.execute(
            "SELECT provider_id,initialized,last_success_at,next_poll_at,error,requests_today FROM provider_state ORDER BY provider_id")],
        "checkpoints": [dict(row) for row in conn.execute(
            "SELECT provider_id,fixture_key,line_number FROM provider_checkpoints ORDER BY provider_id,fixture_key")],
    }


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = self.path.with_name(self.path.name + ".lock").open("a+")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise RuntimeError("another replay worker owns this state path") from None
        try:
            self.conn = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self.conn.executescript("BEGIN IMMEDIATE;" + MIGRATION_1 + "COMMIT;")
                version = 1
            if version == 1:
                self.conn.executescript("BEGIN IMMEDIATE;" + MIGRATION_2 + "COMMIT;")
            elif version != 2:
                raise ValueError("unsupported state schema version")
        except BaseException:
            if hasattr(self, "conn"):
                self.conn.close()
            self._lock.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.conn.close()
        self._lock.close()

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def stats(self) -> dict:
        return _stats(self.conn)

    def status(self) -> dict:
        return _status(self.conn)

    def checkpoint(self, provider_id: str, fixture_key: str) -> int:
        row = self.conn.execute("SELECT line_number FROM provider_checkpoints WHERE provider_id=? AND fixture_key=?",
                                (provider_id, fixture_key)).fetchone()
        return row[0] if row else 0

    def advance(self, provider_id: str, fixture_key: str, line_number: int):
        self.conn.execute("""INSERT INTO provider_checkpoints VALUES(?,?,?)
            ON CONFLICT(provider_id,fixture_key) DO UPDATE SET line_number=excluded.line_number""",
            (provider_id, fixture_key, line_number))

    def previous(self, key: str):
        return self.conn.execute("""SELECT s.*,o.body_json FROM subject_state s
            JOIN observations o ON o.observation_id=s.observation_id WHERE s.subject_key=?""", (key,)).fetchone()

    def history(self, key: str) -> list[dict]:
        return [json.loads(row[0]) for row in self.conn.execute(
            "SELECT body_json FROM observations WHERE subject_key=? AND accepted=1 ORDER BY observed_at,rowid", (key,))]


def health_state(path: Path | str) -> dict:
    """Bounded liveness query: no integrity scan, counts or event ID enumeration."""
    from datetime import datetime, timezone
    from .domain import instant
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3) as conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] != 2:
            return {"healthy": False, "reason": "schema_version"}
        row = conn.execute("SELECT value_json FROM runtime_state WHERE key='heartbeat'").fetchone()
        if row is None:
            return {"healthy": True, "mode": "replay"}
        heartbeat = json.loads(row[0])
        now = datetime.now(timezone.utc)
        age = (now - instant(heartbeat["at"])).total_seconds()
        telegram = conn.execute("SELECT value_json FROM runtime_state WHERE key='telegram_status'").fetchone()
        chat_ok = json.loads(telegram[0]).get("ok", False) if telegram else False
        sources = conn.execute("SELECT last_success_at,error FROM provider_state LIMIT 100").fetchall()
        sources_ok = any(at is not None and (now-instant(at)).total_seconds() <= 3600 for at,error in sources)
        return {"healthy": age <= 180 and chat_ok and sources_ok, "mode": "live", "heartbeat_age_seconds": round(age),
                "telegram_ready": chat_ok, "sources_ready": sources_ok}
