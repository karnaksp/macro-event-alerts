"""Live worker, Telegram diagnostics and offline replay CLI."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import signal
import sqlite3
import sys
import threading
from time import sleep
from pathlib import Path

from .app import replay_file
from .config import load_config, telegram_environment
from .live import LiveWorker
from .render import render_snapshot_messages
from .storage import Store, inspect_state, health_state
from .telegram import TelegramSender
from .validation import ContractError


def snapshot_messages(config: dict) -> list[str]:
    """An explicitly requested saved-data digest, visibly different from new releases."""
    uri = Path(config["service"]["state_path"]).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute("""WITH backfill AS (
            SELECT body_json,observed_at,rowid AS observation_rowid,
                row_number() OVER (PARTITION BY subject_key ORDER BY observed_at DESC,rowid DESC) AS rank
            FROM observations WHERE accepted=0
        )
        SELECT body_json FROM (
            SELECT o.body_json,o.observed_at,o.rowid AS observation_rowid
            FROM subject_state s JOIN observations o ON o.observation_id=s.observation_id
            UNION ALL
            SELECT body_json,observed_at,observation_rowid FROM backfill WHERE rank=1
        ) ORDER BY observed_at DESC,observation_rowid DESC""").fetchall()
    return render_snapshot_messages([json.loads(row[0]) for row in rows], dry_run=config["telegram"]["dry_run"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="macro-event-alerts")
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay", help="offline synthetic replay")
    replay.add_argument("--config", default="config.replay.yaml")
    replay.add_argument("--fixture", default="examples/replay/demo.jsonl")
    replay.add_argument("--state")
    replay.add_argument("--stop-after", type=int)
    replay.add_argument("--no-drain", action="store_true")
    replay.add_argument("--follow", action="store_true")
    replay.add_argument("--interval", type=float, default=30)
    live = sub.add_parser("run", help="poll configured public APIs and deliver live alerts")
    live.add_argument("--config", default="config.live.yaml")
    live.add_argument("--state")
    live.add_argument("--once", action="store_true")
    for name in ("status", "health"):
        child = sub.add_parser(name)
        child.add_argument("--state", default="/data/state.db")
    for name in ("validate-config", "telegram-check", "telegram-test"):
        child = sub.add_parser(name)
        child.add_argument("--config", default="config.live.yaml" if name.startswith("telegram") else "config.replay.yaml")
    args = parser.parse_args(argv)
    try:
        if args.command in ("status", "health"):
            result = inspect_state(args.state) if args.command == "status" else health_state(args.state)
            print(json.dumps(result))
            return 0 if args.command == "status" or result["healthy"] else 2
        config = load_config(args.config, state_path=getattr(args, "state", None))
        if args.command == "validate-config":
            print(json.dumps({"valid": True, "dry_run": config["telegram"]["dry_run"]}))
            return 0
        if args.command.startswith("telegram"):
            sender = TelegramSender(*telegram_environment(config))
            checked = sender.check()
            if args.command == "telegram-check" or not checked["ok"]:
                print(json.dumps(checked))
                return 0 if checked["ok"] else 2
            messages = snapshot_messages(config)
            for page, message in enumerate(messages, start=1):
                if page > 1:
                    sleep(3)
                result = sender.send(message, parse_mode="HTML")
                print(json.dumps({"page": page, "pages": len(messages), "status": result.status,
                                  "message_id": result.message_id, "error_code": result.error_code}))
                if result.status != "delivered":
                    return 2
            return 0
        if args.command == "replay":
            if args.interval <= 0 or (args.stop_after is not None and args.stop_after < 0):
                raise ValueError("interval must be positive and stop-after nonnegative")
            if not config["telegram"]["dry_run"]:
                raise ContractError("replay requires dry_run=true")
        elif config["telegram"]["dry_run"]:
            raise ContractError("run requires live Telegram configuration; use replay for dry-run")
        old_handlers = {}
        if threading.current_thread() is threading.main_thread():
            def stop(signum, frame):
                raise KeyboardInterrupt
            for sig in (signal.SIGTERM, signal.SIGINT):
                old_handlers[sig] = signal.signal(sig, stop)
        try:
            with Store(config["service"]["state_path"]) as store:
                worker = LiveWorker(config, store) if args.command == "run" else None
                while True:
                    if worker is not None:
                        report = worker.tick()
                        # Quiet idle loops; changed provider/Telegram state remains visible.
                        if report["providers"] or report["delivered_attempts"]:
                            print(json.dumps({"type": "live_tick", **report}), flush=True)
                        if args.once:
                            break
                        threading.Event().wait(1)
                    else:
                        report = replay_file(args.config, args.fixture, state_path=args.state, stop_after=args.stop_after,
                                             drain=not args.no_drain, _store=store, _config=config)
                        if report["processed"] or report["counts"]["pending"]:
                            print(json.dumps({"type": "summary", **report}, ensure_ascii=False), flush=True)
                        if not args.follow:
                            break
                        threading.Event().wait(args.interval)
        except KeyboardInterrupt:
            return 0
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        detail = str(exc) if isinstance(exc, ContractError) else type(exc).__name__
        print(json.dumps({"error": detail}), file=sys.stderr)
        return 2
