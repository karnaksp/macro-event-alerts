"""Offline CLI. P1 has no HTTP transport and never reads bot credentials."""
from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
import threading

from .app import replay_file
from .config import load_config
from .storage import Store, inspect_state
from .validation import ContractError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="macro-event-alerts")
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay", help="replay synthetic JSONL using its observation timestamps")
    replay.add_argument("--config", default="config.replay.yaml")
    replay.add_argument("--fixture", default="examples/replay/demo.jsonl")
    replay.add_argument("--state")
    replay.add_argument("--stop-after", type=int)
    replay.add_argument("--no-drain", action="store_true")
    replay.add_argument("--follow", action="store_true", help="wait and resume the same fixture until stopped")
    replay.add_argument("--interval", type=float, default=30)
    for name in ("status", "health"):
        child = sub.add_parser(name)
        child.add_argument("--state", default="/data/state.db")
    validate = sub.add_parser("validate-config")
    validate.add_argument("--config", default="config.replay.yaml")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-config":
            load_config(args.config)
            print(json.dumps({"valid": True, "mode": "dry_run"}))
            return 0
        if args.command in ("status", "health"):
            result = inspect_state(args.state)
            print(json.dumps(result if args.command == "status" else {"healthy": True, "counts": result["counts"]}))
            return 0
        if args.interval <= 0 or (args.stop_after is not None and args.stop_after < 0):
            raise ValueError("interval must be positive and stop-after nonnegative")
        stopped = threading.Event()
        old_handlers = {}
        if threading.current_thread() is threading.main_thread():
            def stop(signum, frame):
                # Python unwinds through Store.transaction; no partial checkpoint survives.
                raise KeyboardInterrupt
            for sig in (signal.SIGTERM, signal.SIGINT):
                old_handlers[sig] = signal.signal(sig, stop)
        try:
            config = load_config(args.config, state_path=args.state)
            with Store(config["service"]["state_path"]) as store:
                while not stopped.is_set():
                    summary = replay_file(args.config, args.fixture, state_path=args.state,
                                          stop_after=args.stop_after, drain=not args.no_drain, _store=store)
                    print(json.dumps({"type": "summary", **summary}, ensure_ascii=False), flush=True)
                    if not args.follow:
                        break
                    stopped.wait(args.interval)
        except KeyboardInterrupt:
            return 0
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        # Do not log input payloads, environment or credentials.
        detail = str(exc) if isinstance(exc, ContractError) else type(exc).__name__
        print(json.dumps({"error": detail}), file=sys.stderr)
        return 2
