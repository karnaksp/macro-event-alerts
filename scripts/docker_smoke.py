"""One offline container, two process executions and one persisted SQLite state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

from macro_event_alerts.storage import inspect_state


def main() -> None:
    assert os.getuid() == 10001, "image must run as the application user"
    assert not Path("/app/.env").exists(), "image must not contain .env"
    assert not any(name.startswith("TELEGRAM_") for name in os.environ), "offline smoke must not receive Telegram credentials"
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory) / "state.db"
        command = ["macro-event-alerts", "replay", "--config", "/app/config.replay.yaml",
                   "--fixture", "/app/examples/replay/demo.jsonl", "--state", str(state)]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        before = inspect_state(state)
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        after = inspect_state(state)
        assert before == after, "restart must preserve counts, event IDs and checkpoint"
        assert after["counts"]["events"] == 7, after["counts"]
        assert after["counts"]["dry_run"] == 6, after["counts"]
        health = subprocess.run(["macro-event-alerts", "health", "--state", str(state)],
                                check=True, capture_output=True, text=True, timeout=10)
        assert json.loads(health.stdout)["healthy"]
        print("Docker smoke PASS: non-root, no credentials, replay process restart preserved state.")
        print(json.dumps(after["counts"], sort_keys=True))


if __name__ == "__main__":
    main()
