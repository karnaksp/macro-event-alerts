"""Live provider polling and durable Telegram delivery; no trading APIs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import random
import time

from .app import _ingest
from .config import telegram_environment
from .domain import canonical, instant, utc
from . import providers
from .storage import Store
from .telegram import TelegramSender
from .validation import ContractError, get_contracts


def state_get(store: Store, key: str, default=None):
    row = store.conn.execute("SELECT value_json FROM runtime_state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def state_set(store: Store, key: str, value):
    store.conn.execute("INSERT INTO runtime_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                       (key, canonical(value)))


def fetch_live(provider_id: str, settings: dict, *, now: datetime, checkpoint=None):
    if settings["adapter"] == "polymarket":
        from .polymarket import fetch
        return fetch(provider_id, settings, now=now, checkpoint=checkpoint)
    return providers.fetch(provider_id, settings, now=now, checkpoint=checkpoint)


def recover_sending(store: Store, now: datetime):
    with store.transaction():
        store.conn.execute("""UPDATE outbox SET status=CASE WHEN attempts>=10 THEN 'dead_letter' ELSE 'ambiguous' END,
            last_error='restart_uncertain',next_attempt_at=CASE WHEN attempts>=10 THEN NULL ELSE ? END
            WHERE delivery_mode='telegram' AND status='sending'""", (utc(now),))


def deliver_outbox(store: Store, sender, *, now: datetime, limit: int = 1, destination: str | None = None) -> int:
    """Claim before network I/O; acknowledge after it. All rows share bot pacing."""
    destination = destination or getattr(sender, "destination", None)
    sent = 0
    for _ in range(limit):
        next_at = state_get(store, "delivery_next_at")
        if next_at and instant(next_at) > now:
            break
        row = store.conn.execute("""SELECT * FROM outbox WHERE delivery_mode='telegram'
            AND status IN ('pending','retry','ambiguous') AND (next_attempt_at IS NULL OR next_attempt_at<=?)
            AND (? IS NULL OR destination=?)
            ORDER BY created_at,rowid LIMIT 1""", (utc(now), destination, destination)).fetchone()
        if row is None:
            break
        attempts = row["attempts"] + 1
        with store.transaction():
            store.conn.execute("UPDATE outbox SET status='sending',attempts=?,sending_started_at=? WHERE outbox_id=?",
                               (attempts, utc(now), row["outbox_id"]))
        result = sender.send(row["message_text"])
        status = result.status
        if attempts >= 10 and status in {"retry", "ambiguous"}:
            status = "dead_letter"
        delay = max(3, 2 ** min(attempts, 8) + random.uniform(0, 1), result.retry_after_seconds or 0)
        delay = min(300, delay) if result.retry_after_seconds is None else delay
        next_attempt = utc(now + timedelta(seconds=delay)) if status in {"retry", "ambiguous"} else None
        with store.transaction():
            store.conn.execute("""UPDATE outbox SET status=?,next_attempt_at=?,last_error=?,message_id=?,sending_started_at=NULL
                WHERE outbox_id=?""", (status, next_attempt, result.error_code, result.message_id, row["outbox_id"]))
            state_set(store, "delivery_next_at", utc(now + timedelta(seconds=max(3, result.retry_after_seconds or 0))))
            if result.error_code in {"telegram_401", "telegram_403"}:
                state_set(store, "telegram_status", {"ok": False, "error_code": result.error_code, "checked_at": utc(now)})
        sent += 1
        if status != "delivered":
            break
    return sent


class LiveWorker:
    def __init__(self, config: dict, store: Store, *, sender=None, clock=None, fetcher=None):
        self.config, self.store = config, store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fetcher = fetcher or fetch_live
        self.contracts = get_contracts()
        self.sender = sender or TelegramSender(*telegram_environment(config))
        _, chat, topic = telegram_environment(config)
        self.destination = "telegram:" + chat + ":" + (topic or "")
        recover_sending(store, self.clock())

    def tick(self, now: datetime | None = None) -> dict:
        explicit_clock = now is not None
        now = now or self.clock()
        report = {"providers": {}, "delivered_attempts": 0}
        with self.store.transaction():
            state_set(self.store, "heartbeat", {"at": utc(now), "mode": "live"})
        chat = state_get(self.store, "telegram_status", {})
        if not chat.get("ok") and (not chat.get("checked_at") or (now-instant(chat["checked_at"])).total_seconds() >= 60):
            chat = {**self.sender.check(), "checked_at": utc(now)}
            with self.store.transaction():
                state_set(self.store, "telegram_status", chat)
        for provider_id, settings in self.config["providers"].items():
            if not settings["enabled"]:
                continue
            with self.store.transaction():
                self.store.conn.execute("INSERT OR IGNORE INTO provider_state(provider_id) VALUES(?)", (provider_id,))
            row = self.store.conn.execute("SELECT * FROM provider_state WHERE provider_id=?", (provider_id,)).fetchone()
            if row["next_poll_at"] and instant(row["next_poll_at"]) > now:
                continue
            date = now.date().isoformat()
            used = row["requests_today"] if row["request_date"] == date else 0
            cost = 2 if settings["adapter"] == "polymarket" else 1
            if used + cost > settings["daily_request_budget"]:
                midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                with self.store.transaction():
                    self.store.conn.execute("UPDATE provider_state SET next_poll_at=?,error='request_budget_exhausted' WHERE provider_id=?", (utc(midnight), provider_id))
                report["providers"][provider_id] = {"error": "request_budget_exhausted"}
                continue
            with self.store.transaction():
                self.store.conn.execute("UPDATE provider_state SET request_date=?,requests_today=?,last_attempt_at=? WHERE provider_id=?",
                                        (date, used+cost, utc(now), provider_id))
            checkpoint = json.loads(row["checkpoint_json"]) if row["checkpoint_json"] else None
            try:
                batch = self.fetcher(provider_id, settings, now=now, checkpoint=checkpoint)
            except (OSError, ValueError, RuntimeError):
                with self.store.transaction():
                    self.store.conn.execute("UPDATE provider_state SET error='provider_fetch_failed',next_poll_at=? WHERE provider_id=?",
                        (utc(now+timedelta(seconds=max(30, settings["poll_interval_seconds"]))), provider_id))
                report["providers"][provider_id] = {"error": "provider_fetch_failed"}
                continue
            receipt = now if explicit_clock else self.clock()
            diagnostics, accepted, quarantined = [], 0, 0
            with self.store.transaction():
                for envelope in batch.envelopes:
                    self.store.conn.execute("SAVEPOINT live_observation")
                    try:
                        if instant(envelope["observation"]["observed_at"]) > receipt:
                            raise ContractError("future_live_observation")
                        info, reason = _ingest(self.store, self.config, self.contracts, envelope, now=receipt)
                        diagnostics.extend(info)
                        self.store.conn.execute("RELEASE live_observation")
                        accepted += 1
                    except (ContractError, KeyError, TypeError, ValueError) as exc:
                        self.store.conn.execute("ROLLBACK TO live_observation")
                        self.store.conn.execute("RELEASE live_observation")
                        reason = str(exc) if isinstance(exc, ContractError) else "invalid_observation_structure"
                        self.store.conn.execute("INSERT INTO quarantine(reason,body_json,observed_at) VALUES(?,?,?)",
                                                (reason, canonical(envelope), utc(receipt)))
                        quarantined += 1
                self.store.conn.execute("""UPDATE provider_state SET initialized=1,last_success_at=?,next_poll_at=?,
                    error=?,checkpoint_json=? WHERE provider_id=?""", (
                    utc(receipt), utc(receipt+timedelta(seconds=settings["poll_interval_seconds"])),
                    "invalid_observations" if quarantined else None, canonical(batch.checkpoint), provider_id))
            report["providers"][provider_id] = {"observations": accepted, "quarantine": quarantined,
                                               "suppressed": len(diagnostics)}
        current = now if explicit_clock else self.clock()
        if chat.get("ok"):
            report["delivered_attempts"] = deliver_outbox(self.store, self.sender, now=current, destination=self.destination)
        with self.store.transaction():
            state_set(self.store, "heartbeat", {"at": utc(current), "mode": "live"})
        report["telegram_ready"] = bool(chat.get("ok"))
        return report
