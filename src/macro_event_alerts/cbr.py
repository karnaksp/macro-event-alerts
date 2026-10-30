"""Opt-in Bank of Russia HTML adapter; no consensus or guessed release times."""
from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4
from zoneinfo import ZoneInfo

from .domain import decimal_text, sha256, utc
from .providers import FetchBatch

PRESS_URL = "https://www.cbr.ru/press/keypr/"
INFLATION_URL = "https://www.cbr.ru/hd_base/infl/"
MOSCOW = ZoneInfo("Europe/Moscow")
_MONTHS = {name: index for index, name in enumerate((
    "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря",
), 1)}
_DATE = r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})\s+года"


def _space(value: str) -> str:
    return " ".join(value.split())


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "macro-event-alerts/0.2"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=20) as response:
            body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ValueError("response_limit")
        return body.decode("utf-8-sig")
    except HTTPError as exc:
        raise RuntimeError(f"cbr_http_{exc.code}") from None
    except (URLError, OSError, ValueError):
        raise RuntimeError("cbr_request_failed") from None


class _Page(HTMLParser):
    """Extract visible headings, paragraphs and table cells; never execute scripts."""
    def __init__(self, document: str):
        super().__init__(convert_charrefs=True)
        self.headlines, self.paragraphs, self.tables, self.visible = [], [], [], []
        self._ignored = 0
        self._heading = self._paragraph = self._table = self._row = self._cell = None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._ignored += 1
        if self._ignored:
            return
        if tag == "h1":
            self._heading = []
        elif tag == "p":
            self._paragraph = []
        elif tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._ignored:
            self._ignored -= 1
            return
        if self._ignored:
            return
        if tag == "h1" and self._heading is not None:
            self.headlines.append(_space(" ".join(self._heading)))
            self._heading = None
        elif tag == "p" and self._paragraph is not None:
            self.paragraphs.append(_space(" ".join(self._paragraph)))
            self._paragraph = None
        elif tag in {"td", "th"} and self._cell is not None:
            self._row.append(_space(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, value):
        if self._ignored:
            return
        self.visible.append(value)
        for container in (self._heading, self._paragraph, self._cell):
            if container is not None:
                container.append(value)


def _number(value: str) -> str:
    value = _space(value).replace(" ", "").replace(",", ".").replace("−", "-")
    if not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        raise ValueError("invalid_number")
    return decimal_text(Decimal(value))


def _date(match) -> datetime:
    return datetime(int(match[3]), _MONTHS[match[2].lower()], int(match[1]), tzinfo=MOSCOW)


def _observation(provider_id: str, raw: dict, *, now: datetime, series_id: str,
                 label: str, period: str, frequency: str, actual: str | None,
                 previous: str | None = None, previous_period: str | None = None,
                 scheduled_at: datetime | None = None, source_updated_at: datetime | None = None) -> dict:
    observation = {
        "schema_version": "1.0", "observation_id": str(uuid4()), "observed_at": utc(now),
        "source": {"provider_id": provider_id, "source_record_id": series_id + ":" + period,
                   "source_revision": None, "source_url": raw["source_url"],
                   "source_updated_at": utc(source_updated_at) if source_updated_at else None,
                   "declared_delay_seconds": None},
        "raw_sha256": sha256(raw), "is_backfill": False,
        "subject": {"kind": "macro", "country": "RU", "currency": "RUB", "series_id": series_id,
                    "label": label, "release_key": series_id + ":" + period, "reference_period": period,
                    "frequency": frequency, "adjustment": "unknown", "unit": "percent", "scale": "units",
                    "transformation": "reported"},
        "values": {"publication_status": "published" if actual is not None else "scheduled",
                   "scheduled_at": utc(scheduled_at) if scheduled_at else None, "published_at": None,
                   "actual": actual, "forecast": None, "previous": previous,
                   "previous_reference_period": previous_period, "forecast_origin": "unknown"},
    }
    return {"raw": raw, "observation": observation}


def _press(provider_id: str, document: str, now: datetime) -> list[dict]:
    page = _Page(document)
    if len(page.headlines) != 1:
        raise ValueError("missing_decision_headline")
    headline = page.headlines[0]
    rate = re.search(r"(?:на уровне|до)\s+([0-9]+(?:[,.][0-9]+)?)\s*%", headline)
    if "ключев" not in headline.lower() or rate is None:
        raise ValueError("missing_decision_rate")
    actual = _number(rate[1])
    if Decimal(actual) < 0:
        raise ValueError("negative_key_rate")
    decisions = [(paragraph, re.search(r"Совет директоров Банка России\s+" + _DATE, paragraph))
                 for paragraph in page.paragraphs]
    decisions = [(paragraph, match) for paragraph, match in decisions if match]
    if len(decisions) != 1:
        raise ValueError("missing_decision_date")
    decision_paragraph, decision_match = decisions[0]
    decision_date = _date(decision_match)
    if decision_date.date() > now.astimezone(MOSCOW).date():
        raise ValueError("future_decision")
    visible = _space(" ".join(page.visible))
    updated_match = re.search(r"Последнее обновление страницы:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)?)", visible)
    updated = None
    if updated_match:
        updated = datetime.fromisoformat(updated_match[1].replace("Z", "+00:00"))
        if updated.tzinfo is None:
            # The next-meeting clock does not establish the footer's timezone.
            updated = None
        if updated and updated > now:
            raise ValueError("future_source_timestamp")
    next_paragraphs = [paragraph for paragraph in page.paragraphs if "Следующее заседание Совета директоров Банка России" in paragraph]
    if len(next_paragraphs) > 1:
        raise ValueError("ambiguous_next_meeting")
    next_paragraph = next_paragraphs[0] if next_paragraphs else None
    raw = {"source_url": PRESS_URL, "headline": headline, "decision": decision_paragraph,
           "next_meeting": next_paragraph, "last_updated": updated_match[1] if updated_match else None}
    result = [_observation(provider_id, raw, now=now, series_id="RU.CBR.KEY_RATE",
        label="Решение Банка России по ключевой ставке", period=decision_date.date().isoformat(),
        frequency="meeting", actual=actual, source_updated_at=updated)]
    if next_paragraph:
        date_match = re.search(r"запланировано на\s+" + _DATE, next_paragraph)
        clock_match = re.search(r"Время публикации пресс-релиза[^.]*?([01]?[0-9]|2[0-3]):([0-5][0-9])\s+по московскому времени", next_paragraph)
        if date_match and clock_match:
            next_date = _date(date_match)
            scheduled = datetime.combine(next_date.date(), time(int(clock_match[1]), int(clock_match[2])), MOSCOW)
            if scheduled > now:
                result.append(_observation(provider_id, raw, now=now, series_id="RU.CBR.KEY_RATE",
                    label="Решение Банка России по ключевой ставке", period=scheduled.date().isoformat(),
                    frequency="meeting", actual=None, previous=actual,
                    previous_period=decision_date.date().isoformat(), scheduled_at=scheduled,
                    source_updated_at=updated))
    return result


def _inflation(provider_id: str, document: str, now: datetime) -> dict:
    page = _Page(document)
    tables = []
    for rows in page.tables:
        if not rows:
            continue
        columns = [index for index, header in enumerate(rows[0])
                   if "инфляция" in header.lower() and "г/г" in header.lower() and "цель" not in header.lower()]
        if len(columns) == 1 and rows[0][0].lower() == "дата":
            tables.append((rows, columns[0]))
    if len(tables) != 1:
        raise ValueError("missing_inflation_table")
    rows, column = tables[0]
    parsed = []
    for row in rows[1:]:
        if len(row) != len(rows[0]):
            raise ValueError("invalid_inflation_row")
        match = re.fullmatch(r"(0[1-9]|1[0-2])\.(\d{4})", row[0])
        if not match:
            raise ValueError("invalid_inflation_period")
        period = f"{match[2]}-{match[1]}"
        if datetime(int(match[2]), int(match[1]), 1, tzinfo=MOSCOW) > now:
            raise ValueError("future_inflation_period")
        parsed.append((period, _number(row[column]), row))
    parsed.sort(reverse=True)
    if not parsed or len({row[0] for row in parsed}) != len(parsed):
        raise ValueError("missing_or_duplicate_inflation")
    period, actual, row = parsed[0]
    previous_period, previous = (parsed[1][0], parsed[1][1]) if len(parsed) > 1 else (None, None)
    raw = {"source_url": INFLATION_URL, "columns": rows[0], "row": row,
           "previous_row": parsed[1][2] if len(parsed) > 1 else None}
    return _observation(provider_id, raw, now=now, series_id="RU.CPI.HEADLINE.YOY",
        label="Инфляция CPI России · год к году", period=period, frequency="monthly",
        actual=actual, previous=previous, previous_period=previous_period)


def fetch(provider_id: str, settings: dict, *, now: datetime, checkpoint=None) -> FetchBatch:
    if not settings.get("experimental"):
        raise ValueError("cbr_web requires experimental=true")
    if now.tzinfo is None:
        raise ValueError("provider clock requires timezone")
    now = now.astimezone(timezone.utc)
    try:
        press = _request_text(PRESS_URL)
        inflation = _request_text(INFLATION_URL)
    except Exception:
        raise RuntimeError("cbr_fetch_failed") from None
    try:
        envelopes = _press(provider_id, press, now) + [_inflation(provider_id, inflation, now)]
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise RuntimeError("cbr_data_invalid") from None
    return FetchBatch(envelopes)
