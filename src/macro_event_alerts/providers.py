"""Public macro-calendar adapters. MQL5 web is undocumented and opt-in.

Each fetch makes at most one HTTP request. Calendar timestamps are scheduled
release times, never invented publication/update times. Missing periods in the
MQL5 listing stay explicit ``unknown`` and its vendor release ID scopes identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from .domain import decimal_text, instant, sha256, utc
from .catalog import MQL5_REGIONS


@dataclass
class FetchBatch:
    envelopes: list[dict]
    checkpoint: dict | None = None


# Exact source slugs keep headline/core, MoM/YoY and rate conventions separate.
# GDP's annualized basis corroborated with the BEA 2026-Q3 release, 2026-10-30.
MQL5_SERIES = MQL5_REGIONS["US"]["series"]


def capabilities(adapter: str) -> set[str]:
    if adapter in {"fixture", "mql5_web"}:
        return {"macro.release", "macro.forecast_changed", "macro.revised"} | (
            {"scenario.probability_changed"} if adapter == "fixture" else set()
        )
    if adapter in {"xoomar", "cbr_web"}:
        return {"macro.release", "macro.revised"}
    return set()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request_json(url: str, *, data: bytes | None = None, headers: dict | None = None) -> tuple[object, dict, int]:
    request = Request(url, data=data, headers={"User-Agent": "macro-event-alerts/0.1", **(headers or {})})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=20) as response:
            body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise RuntimeError("provider response exceeded size limit")
            return json.loads(body), dict(response.headers.items()), response.status
    except HTTPError as exc:
        if exc.code == 304:
            return None, dict(exc.headers.items()), 304
        raise RuntimeError(f"provider HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise RuntimeError("provider request or JSON decoding failed") from None


def _window(options: dict, now: datetime) -> tuple[datetime, datetime]:
    before, after = options.get("look_back_days", 2), options.get("look_ahead_days", 14)
    if type(before) is not int or type(after) is not int or before < 0 or after < 0 or before + after > 31:
        raise ValueError("calendar lookback/lookahead must be nonnegative integers totalling at most 31 days")
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=before), today + timedelta(days=after + 1)


def _number(value, unit: str, *, suffixed: bool = False) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("source numbers must be strings")
    value = value.replace("\u00a0", "").replace("\u202f", "").replace("\u2212", "-").replace(" ", "").strip()
    if value in {"", "-", "—", "N/A"}:
        return None
    if unit == "percent":
        if suffixed and not value.endswith("%"):
            raise ValueError("percent suffix missing")
        value = value.removesuffix("%")
    factor = Decimal(1)
    if unit == "persons" and suffixed and value[-1:] in {"K", "M", "B"}:
        factor = {"K": Decimal(1000), "M": Decimal(1000000), "B": Decimal(1000000000)}[value[-1]]
        value = value[:-1]
    if not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        raise ValueError("invalid numeric string")
    try:
        return decimal_text(Decimal(value) * factor)
    except InvalidOperation:
        raise ValueError("invalid decimal") from None


def _envelope(provider_id: str, raw: dict, *, now: datetime, record_id: str, source_url: str,
              series: tuple[str, str, str], reference_period: str, scheduled_at: datetime,
              actual: str | None, forecast: str | None, previous: str | None, scale: str = "units",
              country: str = "US", currency: str = "USD") -> dict:
    observation = {
        "schema_version": "1.0", "observation_id": str(uuid4()), "observed_at": utc(now),
        "source": {"provider_id": provider_id, "source_record_id": record_id, "source_revision": None,
                   "source_url": source_url, "source_updated_at": None, "declared_delay_seconds": None},
        "raw_sha256": sha256(raw), "is_backfill": False,
        "subject": {"kind": "macro", "country": country, "currency": currency, "series_id": series[0],
                    "label": str(raw.get("EventName", raw.get("eventName", series[0]))),
                    "release_key": record_id, "reference_period": reference_period,
                    "frequency": series[1], "adjustment": "unknown", "unit": series[2],
                    "scale": scale, "transformation": "reported"},
        "values": {"publication_status": "published" if actual is not None else "scheduled",
                   "scheduled_at": utc(scheduled_at), "published_at": None, "actual": actual,
                   "forecast": forecast, "previous": previous, "previous_reference_period": None,
                   "forecast_origin": "unknown"},
    }
    return {"raw": raw, "observation": observation}


def _mql5(provider_id: str, settings: dict, now: datetime) -> FetchBatch:
    if not settings.get("experimental"):
        raise ValueError("mql5_web requires experimental=true")
    options = settings.get("options", {})
    countries = options.get("countries", ["US"])
    if (not isinstance(countries, list) or not countries
            or any(not isinstance(code, str) or code not in MQL5_REGIONS for code in countries)
            or len(set(countries)) != len(countries)):
        raise ValueError("unsupported or duplicate MQL5 countries")
    paths = {MQL5_REGIONS[code]["path"]: (code, MQL5_REGIONS[code]) for code in countries}
    start, end = _window(options, now)
    form = urlencode({"date_mode": 1, "from": start.strftime("%Y-%m-%dT%H:%M:%S"),
                      "to": (end - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S"),
                      "importance": 15, "currencies": 131071}).encode()
    data, _, _ = _request_json("https://www.mql5.com/en/economic-calendar/content", data=form, headers={
        "User-Agent": "Mozilla/5.0 (compatible; MacroEventAlerts/0.1)",
        "Content-Type": "application/x-www-form-urlencoded", "Origin": "https://www.mql5.com",
        "Referer": "https://www.mql5.com/en/economic-calendar", "X-Requested-With": "XMLHttpRequest",
    })
    if not isinstance(data, list):
        raise RuntimeError("MQL5 calendar response must be an array")
    result = []
    for row in data:
        if not isinstance(row, dict):
            continue
        path = row.get("Url", "")
        if not isinstance(path, str) or not isinstance(row.get("EventName"), str):
            continue
        parts = path.split("/")
        if len(parts) != 5 or parts[:3] != ["", "en", "economic-calendar"] or parts[3] not in paths:
            continue
        country, region = paths[parts[3]]
        slug = parts[4]
        if (slug not in region["series"] or row.get("Country") != region["source_country_id"]
                or row.get("CurrencyCode") != region["currency"]):
            continue
        try:
            if row.get("TimeMode") != 0 or type(row.get("Id")) is not int or row["Id"] < 1:
                continue
            # FullDate is UTC for this transport, corroborated with its Unix-ms field.
            scheduled = datetime.fromisoformat(row["FullDate"])
            scheduled = scheduled.replace(tzinfo=timezone.utc) if scheduled.tzinfo is None else scheduled.astimezone(timezone.utc)
            epoch = datetime.fromtimestamp(row["ReleaseDate"] / 1000, timezone.utc)
            if abs((scheduled - epoch).total_seconds()) > 1 or not start <= scheduled < end:
                continue
            series = region["series"][slug]
            actual = _number(row.get("ActualValue"), series[2], suffixed=True)
            forecast = _number(row.get("ForecastValue"), series[2], suffixed=True)
            previous = _number(row.get("PreviousValue"), series[2], suffixed=True)
            if actual is not None and (row.get("Processed") != 1 or scheduled > now):
                continue
            if row.get("Processed") == 1 and actual is None:
                continue  # A released-but-missing actual cannot become a pre-release forecast update.
            result.append(_envelope(provider_id, row, now=now, record_id=f"mql5:{row['Id']}",
                source_url="https://www.mql5.com" + path, series=series, reference_period="unknown",
                scheduled_at=scheduled, actual=actual, forecast=forecast, previous=previous,
                country=country, currency=region["currency"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return FetchBatch(result)


_MONTHS = {name: index for index, name in enumerate(("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}


def _period(label: str | None, event_name: str) -> str | None:
    text = label or event_name
    match = re.fullmatch(r"([A-Za-z]+) (\d{4})", text)
    if match and match[1] in _MONTHS:
        return f"{match[2]}-{_MONTHS[match[1]]:02}"
    match = re.search(r"(?i)(First|Second|Third|Fourth|[1-4](?:st|nd|rd|th)) Quarter (\d{4})", text)
    if match:
        quarter = {"first": 1, "second": 2, "third": 3, "fourth": 4}.get(match[1].lower())
        return f"{match[2]}-Q{quarter or match[1][0]}"
    return label.strip() if isinstance(label, str) and "meeting" in label.lower() else None


def _xoomar_series(row: dict) -> tuple[tuple[str, str, str], str] | None:
    name, unit = row.get("eventName", ""), row.get("unit")
    if not isinstance(name, str):
        return None
    mappings = {
        "CPI (Consumer Price Index)": (("US.CPI.HEADLINE.YOY", "monthly", "percent"), "units", "% y/y, all items"),
        "Personal Income and Outlays (PCE)": (("US.PCE.HEADLINE.YOY", "monthly", "percent"), "units", "% y/y, PCE price index"),
        "Nonfarm Payrolls (Employment Situation)": (("US.NFP.CHANGE", "monthly", "persons"), "thousands", "thousands, monthly change"),
        "FOMC Rate Decision": (("US.FED.TARGET.MIDPOINT", "meeting", "percent"), "units", "%, target range midpoint"),
        "Employment Cost Index": (("US.ECI.QOQ", "quarterly", "percent"), "units", "% q/q"),
    }
    selected = mappings.get(name)
    if name.startswith("GDP (") and "Quarter" in name:
        selected = (("US.GDP.QOQ.ANNUALIZED", "quarterly", "percent"), "units", "% q/q annualized, real GDP")
    if selected is None:
        return None
    # A pending known series can have no values/unit yet. Measured values need a verified basis.
    if unit != selected[2] and not (unit is None and row.get("actual") is None and row.get("previous") is None):
        return None
    return selected[0], selected[1]


def _xoomar(provider_id: str, settings: dict, now: datetime, checkpoint: dict | None) -> FetchBatch:
    start, end = _window(settings.get("options", {}), now)
    headers = {}
    url = "https://xoomar.com/api/markets/calendar?" + urlencode({"from": start.date().isoformat(), "to": (end - timedelta(days=1)).date().isoformat()})
    if checkpoint and checkpoint.get("request_url") == url and isinstance(checkpoint.get("etag"), str):
        headers["If-None-Match"] = checkpoint["etag"]
    data, response_headers, status = _request_json(url, headers=headers)
    if status == 304:
        return FetchBatch([], checkpoint)
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise RuntimeError("XOOMAR calendar response must contain a data array")
    result = []
    for row in data["data"]:
        if not isinstance(row, dict):
            continue
        selected = _xoomar_series(row)
        if selected is None:
            continue
        try:
            period = _period(row.get("periodLabel"), row["eventName"])
            if period is None:
                continue
            scheduled = instant(row["scheduledAt"]).astimezone(timezone.utc)
            if not start <= scheduled < end:
                continue
            series, scale = selected
            actual = _number(row.get("actual"), series[2])
            if actual is not None and scheduled > now:
                continue
            previous = _number(row.get("previous"), series[2])
            record_id = "xoomar:" + sha256({"source": row.get("source"), "name": row["eventName"], "period": period})
            source_url = row.get("canonicalUrl", "https://xoomar.com/markets/calendar")
            if not isinstance(source_url, str) or not source_url.startswith("https://xoomar.com/markets/"):
                source_url = "https://xoomar.com/markets/calendar"
            result.append(_envelope(provider_id, row, now=now, record_id=record_id,
                source_url=source_url, series=series, reference_period=period, scheduled_at=scheduled,
                actual=actual, forecast=None, previous=previous, scale=scale))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    etag = next((value for key, value in response_headers.items() if key.lower() == "etag"), None)
    return FetchBatch(result, {"etag": etag, "request_url": url, "next_release_at": data.get("nextReleaseAt")})


def fetch(provider_id: str, settings: dict, *, now: datetime, checkpoint: dict | None = None) -> FetchBatch:
    if now.tzinfo is None:
        raise ValueError("provider clock requires timezone")
    now = now.astimezone(timezone.utc)
    adapter = settings.get("adapter")
    if adapter == "mql5_web":
        return _mql5(provider_id, settings, now)
    if adapter == "xoomar":
        return _xoomar(provider_id, settings, now, checkpoint)
    if adapter == "cbr_web":
        from .cbr import fetch as fetch_cbr
        return fetch_cbr(provider_id, settings, now=now, checkpoint=checkpoint)
    raise ValueError("unsupported live macro adapter")
