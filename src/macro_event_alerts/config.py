"""Configuration/capabilities are checked once at process startup."""
from __future__ import annotations

import os
from pathlib import Path
import yaml

from .providers import capabilities
from .validation import get_contracts, ContractError


def load_config(path: Path | str, *, state_path: Path | str | None = None) -> dict:
    config = yaml.safe_load(Path(path).read_text())
    get_contracts().validate(config, "config.schema.json")
    enabled = {key: value for key, value in config["providers"].items() if value["enabled"]}
    if not enabled:
        raise ContractError("enable at least one provider")
    for provider_id, provider in enabled.items():
        adapter = provider["adapter"]
        available = {"scenario.probability_changed"} if adapter == "polymarket" else capabilities(adapter)
        if not available:
            raise ContractError(f"provider {provider_id}: adapter is not implemented")
        if adapter == "fixture" and not config["telegram"]["dry_run"]:
            raise ContractError("fixture observations cannot use live Telegram delivery")
    for rule in config["rules"]:
        if not rule["enabled"]:
            continue
        for provider_id in rule["provider_ids"]:
            adapter = config["providers"][provider_id]["adapter"]
            available = {"scenario.probability_changed"} if adapter == "polymarket" else capabilities(adapter)
            if not set(rule["event_types"]) <= available:
                raise ContractError(f"rule {rule['id']}: provider {provider_id} lacks required capability")
    if state_path is not None:
        config["service"]["state_path"] = str(state_path)
    return config


def telegram_environment(config: dict) -> tuple[str, str, str | None]:
    settings = config["telegram"]
    token = os.environ.get(settings["bot_token_env"], "")
    chat = os.environ.get(settings["chat_id_env"], "")
    topic_key = settings["message_thread_id_env"]
    topic = os.environ.get(topic_key, "") if topic_key else None
    if not token or not chat:
        raise ContractError("Telegram token/chat environment variables are required for live delivery")
    return token, chat, topic or None
