"""Configuration checks; only the offline fixture adapter is implemented in P1."""
from __future__ import annotations

from pathlib import Path
import yaml

from .validation import Contracts, ContractError

FIXTURE_CAPABILITIES = {"macro.release", "macro.forecast_changed", "macro.revised", "scenario.probability_changed"}


def load_config(path: Path | str, *, state_path: Path | str | None = None) -> dict:
    config = yaml.safe_load(Path(path).read_text())
    Contracts().validate(config, "config.schema.json")
    if not config["telegram"]["dry_run"]:
        raise ContractError("P1 supports dry_run=true only; live Telegram delivery is not implemented")
    enabled = {key: value for key, value in config["providers"].items() if value["enabled"]}
    if not enabled:
        raise ContractError("enable at least one fixture provider")
    for provider_id, provider in enabled.items():
        if provider["adapter"] != "fixture":
            raise ContractError(f"provider {provider_id}: adapter is not implemented in P1; use config.replay.yaml")
    for rule in config["rules"]:
        if rule["enabled"] and not set(rule["event_types"]) <= FIXTURE_CAPABILITIES:
            raise ContractError(f"rule {rule['id']}: unsupported capability")
    if state_path is not None:
        config["service"]["state_path"] = str(state_path)
    return config
