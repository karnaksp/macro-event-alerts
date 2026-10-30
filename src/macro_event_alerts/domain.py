"""Shared deterministic identity and Decimal helpers."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

MACRO_IDENTITY = (
    "kind", "country", "currency", "series_id", "release_key", "reference_period",
    "frequency", "adjustment", "unit", "scale", "transformation",
)
SCENARIO_IDENTITY = (
    "kind", "contract_id", "outcome_id", "horizon_at", "resolution_hash", "price_basis",
)


def canonical(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256(data: object) -> str:
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()


def subject_key(data: dict) -> str:
    subject = data["subject"]
    fields = MACRO_IDENTITY if subject["kind"] == "macro" else SCENARIO_IDENTITY
    return sha256({"provider_id": data["source"]["provider_id"], "subject": {key: subject[key] for key in fields}})


def instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp requires a timezone")
    return result


def utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
