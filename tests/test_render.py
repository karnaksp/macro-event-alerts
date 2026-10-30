"""User-facing facts, units and safe Telegram markup for alert templates."""
from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
import re

import pytest

from macro_event_alerts.render import render, render_snapshot


ROOT = Path(__file__).resolve().parents[1]


def fixture(name: str, *, observation: bool = False) -> dict:
    folder = "observations" if observation else "events"
    return json.loads((ROOT / "examples" / folder / f"{name}.json").read_text())


class MessageHTML(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[str] = []
        self.tags: list[str] = []
        self.stack: list[str] = []
        self.feed(text)
        assert not self.stack, "message ended inside a formatting tag"

    def handle_starttag(self, tag: str, attrs):
        assert tag in {"b", "i", "a", "code", "s", "u"}, f"unexpected Telegram tag: {tag}"
        self.tags.append(tag)
        self.stack.append(tag)
        if tag == "a":
            self.links.append(dict(attrs)["href"])

    def handle_endtag(self, tag: str):
        assert self.stack.pop() == tag, "unbalanced Telegram markup"

    def handle_data(self, data: str):
        self.text.append(data)

    @property
    def visible(self) -> str:
        return "".join(self.text)


def view(event: dict, context: dict | None = None) -> MessageHTML:
    return MessageHTML(render(event, context or {}, dry_run=False))


def test_release_keeps_facts_units_period_and_human_publication_time():
    event = fixture("macro.release")
    event["source"]["provider_id"] = "mql5_web"
    event["source"]["source_url"] = "https://www.mql5.com/en/economic-calendar/united-states/cpi-yy"
    message = view(event)
    text = message.visible
    assert "CPI" in text and "США" in text
    assert "3,1%" in text and re.search(r"3(?:,0)?%", text) and "2,9%" in text
    assert "0,1 п.п." in text
    assert "2026" in text and ("сентябр" in text.lower() or "09.2026" in text)
    assert "14:30" in text and any(zone in text for zone in ("CEST", "Подгорица"))
    assert message.links == [event["source"]["source_url"]]
    assert "b" in message.tags
    assert "https://" not in text
    assert all(internal not in text for internal in ("mql5_web", "percent", "units", "T12:30:00Z", event["event_id"]))


def test_zero_is_a_fact_and_missing_forecast_is_not_zero():
    event = fixture("macro.release")
    event["payload"].update(actual="0", forecast=None)
    text = view(event).visible
    assert "0%" in text
    assert re.search(r"[Пп]рогноз[^\n]*?(?:нет данных|—)", text)
    assert not re.search(r"[Пп]рогноз\s*:\s*0%", text)
    assert "2,9%" in text


def test_employment_uses_people_and_thousands_instead_of_percent():
    event = fixture("macro.release")
    event["subject"].update(series_id="US.NFP.CHANGE", unit="persons", scale="thousands")
    event["payload"].update(actual="125", forecast="100", previous="90")
    text = view(event).visible
    assert re.search(r"125\s*тыс\.?\s*чел", text)
    assert "125%" not in text and "25 п.п." not in text


@pytest.mark.parametrize("name,before,after,kind", [
    ("macro.forecast_changed", "2,9%", "3%", "прогноз"),
    ("macro.revised", "3,1%", "3,2%", "пересмотр"),
])
def test_changes_show_before_after_and_the_right_event_kind(name, before, after, kind):
    text = view(fixture(name)).visible
    assert before in text and after in text and "→" in text
    assert kind in text.lower()
    assert "origin:" not in text and "unknown" not in text


def test_scenario_uses_saved_window_and_preserves_subpercent_probability():
    event = fixture("scenario.probability_changed")
    event["subject"]["outcome_id"] = "1234567890" * 7
    event["subject"]["outcome_label"] = "Yes"
    context = {
        "before": "0.0055", "after": "0.0155", "delta_pp": "1",
        "window_start": "2026-10-14T12:31:00Z", "window_end": "2026-10-14T13:31:00Z",
        "current_observed_at": "2026-10-14T13:31:00Z",
    }
    text = view(event, context).visible
    assert "0,55%" in text and "1,55%" in text
    assert "+1 п.п." in text
    assert "55% → 75%" not in text
    assert event["subject"]["outcome_id"] not in text
    assert "Исход: Да" in text
    assert "midpoint" not in text


def test_legacy_scenario_preserves_outcome_identity_when_label_is_absent():
    event = fixture("scenario.probability_changed")
    context = {"before": "0.55", "after": "0.75", "delta_pp": "20"}
    assert event["subject"]["outcome_id"] in view(event, context).visible


def test_source_label_and_link_cannot_inject_telegram_markup():
    event = fixture("macro.release")
    event["subject"].update(series_id="CUSTOM.SERIES", label='<i>Значение & "тест"</i>')
    event["source"]["source_url"] = "https://example.com/calendar?a=1&b=%22two%22"
    raw = render(event, {}, dry_run=False)
    message = MessageHTML(raw)
    assert '<i>Значение & "тест"</i>' in message.visible
    assert "&lt;i&gt;" in raw and "&amp;" in raw
    assert message.links == [event["source"]["source_url"]]


def test_snapshot_is_explicit_history_with_one_missing_period_caveat():
    items = []
    for series, actual in (("US.GDP.QOQ.ANNUALIZED", "3.4"), ("US.PCE.HEADLINE.YOY", "3.6"), ("US.PCE.HEADLINE.MOM", "0.5")):
        item = fixture("macro", observation=True)
        item["subject"].update(series_id=series, reference_period="unknown")
        item["values"].update(actual=actual, published_at=None, scheduled_at="2026-10-29T12:30:00Z")
        item["source"].update(provider_id="mql5_web", source_url="https://www.mql5.com/en/economic-calendar")
        items.append(item)
    message = MessageHTML(render_snapshot(items, at="2026-10-30T11:00:00Z"))
    text = message.visible
    assert "снимок" in text.lower()
    assert all(value in text for value in ("3,4%", "3,6%", "0,5%"))
    assert "ВВП" in text and "PCE" in text
    assert text.lower().count("период") == 1
    assert "2026-Q" not in text and "сентябрь" not in text.lower()
    assert "календар" in text.lower()
    assert all(internal not in text for internal in ("baseline", "units", "mql5_web", "T12:30:00Z"))
    assert "https://" not in text


def test_dry_run_message_and_snapshot_do_not_claim_live_delivery():
    event = fixture("macro.release")
    text = MessageHTML(render(event, {}, dry_run=True)).visible.lower()
    snapshot = MessageHTML(render_snapshot([fixture("macro", observation=True)], dry_run=True)).visible.lower()
    for message in (text, snapshot):
        assert any(marker in message for marker in ("тест", "replay", "синтетичес", "dry-run"))
        assert "live-алерты включены" not in message

