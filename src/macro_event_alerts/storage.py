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


def inspect_state(path: Path | str) -> dict:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("unsupported state schema version")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        return _status(conn)


def _stats(conn: sqlite3.Connection) -> dict:
    counts = {name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
              for name in ("observations", "events", "outbox", "quarantine")}
    for status in ("pending", "dry_run"):
        counts[status] = conn.execute("SELECT count(*) FROM outbox WHERE status=?", (status,)).fetchone()[0]
    return counts


def _status(conn: sqlite3.Connection) -> dict:
    return {
        "counts": _stats(conn),
        "event_ids": [row[0] for row in conn.execute("SELECT event_id FROM events ORDER BY rowid")],
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
            elif version != 1:
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
