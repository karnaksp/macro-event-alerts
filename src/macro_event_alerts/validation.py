"""Offline JSON Schema validation shared by runtime and contract checks."""
from __future__ import annotations

import json
from pathlib import Path
from decimal import Decimal

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .domain import subject_key, instant


class ContractError(ValueError):
    pass


def semantics(data: dict) -> list[str]:
    """Only static invariants; no baseline, network, database or delivery checks."""
    errors = []
    observed = instant(data["observed_at"])
    source_time = data["source"]["source_updated_at"]
    if source_time is not None and instant(source_time) > observed:
        errors.append("source_updated_at cannot follow observed_at")
    publication = (data.get("payload", {}) if "event_type" in data else data.get("values", {})).get("published_at")
    if publication is not None and instant(publication) > observed:
        errors.append("published_at cannot follow observed_at")
    if "event_type" not in data:
        if data.get("subject", {}).get("kind") == "scenario":
            values = data["values"]
            if instant(values["sample_at"]) > instant(data["observed_at"]):
                errors.append("sample_at cannot follow observed_at")
            bid, ask = values["best_bid"], values["best_ask"]
            if bid is not None and ask is not None:
                if Decimal(bid) > Decimal(ask):
                    errors.append("best_bid cannot exceed best_ask")
                if data["subject"]["price_basis"] == "midpoint":
                    if Decimal(values["probability"]) != (Decimal(bid) + Decimal(ask)) / 2:
                        errors.append("midpoint observation must match current book")
            elif data["subject"]["price_basis"] == "midpoint":
                errors.append("midpoint observation requires both book sides")
        return errors
    if data["subject_key"] != subject_key(data):
        errors.append("subject_key does not match canonical identity")
    payload = data["payload"]
    event_type = data["event_type"]
    if event_type in ("macro.forecast_changed", "scenario.probability_changed"):
        if Decimal(payload["before"]) == Decimal(payload["after"]):
            errors.append("change event has equal before and after")
    if event_type == "macro.revised":
        if Decimal(payload["before_actual"]) == Decimal(payload["after_actual"]):
            errors.append("revision has equal before_actual and after_actual")
    if "before_observed_at" in payload:
        before = instant(payload["before_observed_at"])
        after = instant(payload["after_observed_at"])
        if before >= after:
            errors.append("before_observed_at must precede after_observed_at")
        if after != instant(data["observed_at"]):
            errors.append("event observed_at must match after_observed_at")
    if event_type == "scenario.probability_changed":
        expected_pp = (Decimal(payload["after"]) - Decimal(payload["before"])) * 100
        if Decimal(payload["delta_pp"]) != expected_pp:
            errors.append("delta_pp must equal (after - before) * 100")
        for prefix in ("before", "after"):
            if instant(payload[f"{prefix}_sample_at"]) > instant(payload[f"{prefix}_observed_at"]):
                errors.append(f"{prefix}_sample_at cannot follow observation")
        bid, ask = payload["best_bid"], payload["best_ask"]
        if bid is not None and ask is not None:
            if Decimal(bid) > Decimal(ask):
                errors.append("best_bid cannot exceed best_ask")
            if data["subject"]["price_basis"] == "midpoint":
                if Decimal(payload["after"]) != (Decimal(bid) + Decimal(ask)) / 2:
                    errors.append("midpoint must match current book")
        elif data["subject"]["price_basis"] == "midpoint":
            errors.append("midpoint requires both book sides")
    return errors


def config_semantics(config: dict) -> list[str]:
    errors = []
    known = set(config["providers"])
    seen = set()
    for provider_id, provider in config["providers"].items():
        if provider["adapter"] == "mql5_web" and not provider["experimental"]:
            errors.append(f"mql5_web must be experimental: {provider_id}")
    for rule in config["rules"]:
        if rule["id"] in seen:
            errors.append(f"duplicate rule ID: {rule['id']}")
        seen.add(rule["id"])
        if not set(rule["provider_ids"]) <= known:
            errors.append(f"unknown provider in rule: {rule['id']}")
        scenario = any(t.startswith("scenario.") for t in rule["event_types"])
        macro = any(t.startswith("macro.") for t in rule["event_types"])
        if scenario and macro:
            errors.append(f"mixed macro/scenario rule: {rule['id']}")
        if scenario and (not rule["contract_ids"] or rule["series_ids"]):
            errors.append(f"scenario rule requires only contract IDs: {rule['id']}")
        if macro and (not rule["series_ids"] or rule["contract_ids"]):
            errors.append(f"macro rule requires only series IDs: {rule['id']}")
        if scenario and (rule["min_change_pp"] is None or Decimal(rule["min_change_pp"]) <= 0):
            errors.append(f"scenario threshold must be positive: {rule['id']}")
        if macro and rule["min_change_pp"] is not None:
            errors.append(f"macro threshold in pp is unsupported: {rule['id']}")
        if rule["enabled"]:
            if any(not config["providers"][p]["enabled"] for p in rule["provider_ids"] if p in known):
                errors.append(f"enabled rule uses disabled provider: {rule['id']}")
            if any("REPLACE_" in c for c in rule["contract_ids"]):
                errors.append(f"enabled rule uses placeholder contract: {rule['id']}")
    return errors



class Contracts:
    def __init__(self, directory: Path | None = None):
        if directory is None:
            packaged = Path(__file__).parent / "contracts/v1"
            directory = packaged if packaged.is_dir() else Path(__file__).resolve().parents[2] / "contracts/v1"
        self.schemas = {}
        registry = Registry()
        for path in sorted(directory.glob("*.schema.json")):
            schema = json.loads(path.read_text())
            Draft202012Validator.check_schema(schema)
            self.schemas[path.name] = schema
            registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        self.validators = {
            name: Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
            for name, schema in self.schemas.items()
        }

    def validate(self, data: dict, schema_name: str) -> None:
        errors = list(self.validators[schema_name].iter_errors(data))
        if errors:
            # Values from inputs never appear in diagnostics (they may contain credentials).
            error = errors[0]
            location = ".".join(str(p) for p in error.absolute_path) or "root"
            raise ContractError(f"{schema_name}: {location} failed {error.validator}")
        details = config_semantics(data) if schema_name == "config.schema.json" else semantics(data)
        if details:
            raise ContractError("; ".join(details))
