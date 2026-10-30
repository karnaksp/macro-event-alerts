"""User-facing facts, units and safe Telegram markup for alert templates."""
from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
import re

import pytest

from macro_event_alerts.render import render, render_snapshot_messages


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


def test_ism_pmi_uses_index_points_for_values_and_forecast_difference():
    event = fixture("macro.release")
    event["subject"].update(series_id="US.ISM.MANUFACTURING.PMI", unit="index", scale="units")
    event["payload"].update(actual="52.4", forecast="51.2", previous="50.9")
    text = view(event).visible
    assert "ISM" in text and "США" in text
    assert "52,4 пунктов" in text and "51,2 пунктов" in text and "50,9 пунктов" in text
    assert "1,2 пунктов" in text
    assert "%" not in text and "п.п." not in text


def test_initial_claims_use_person_counts_instead_of_percent():
    event = fixture("macro.release")
    event["subject"].update(series_id="US.JOBLESS.INITIAL.CLAIMS", unit="persons", scale="units")
    event["payload"].update(actual="218000", forecast="220000", previous="223000")
    text = view(event).visible
    assert "заяв" in text.lower() and "США" in text
    assert re.search(r"218\s*000\s*чел", text)
    assert re.search(r"220\s*000\s*чел", text)
    assert re.search(r"223\s*000\s*чел", text)
    assert re.search(r"2\s*000\s*чел", text)
    assert "%" not in text and "п.п." not in text


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


def test_snapshot_is_explicit_history_and_does_not_invent_missing_periods():
    items = []
    for series, actual in (("US.GDP.QOQ.ANNUALIZED", "3.4"), ("US.PCE.HEADLINE.YOY", "3.6"), ("US.PCE.HEADLINE.MOM", "0.5")):
        item = fixture("macro", observation=True)
        item["subject"].update(series_id=series, reference_period="unknown")
        item["values"].update(actual=actual, published_at=None, scheduled_at="2026-10-29T12:30:00Z")
        item["source"].update(provider_id="mql5_web", source_url="https://www.mql5.com/en/economic-calendar")
        items.append(item)
    messages = [MessageHTML(page) for page in render_snapshot_messages(items, at="2026-10-30T11:00:00Z")]
    text = "\n".join(message.visible for message in messages)
    assert any(label in text.lower() for label in ("снимок", "сводка"))
    assert all(value in text for value in ("3,4%", "3,6%", "0,5%"))
    assert "ВВП" in text and "PCE" in text
    assert "период" in text.lower()
    assert "2026-Q" not in text and "сентябрь" not in text.lower()
    assert "календар" in text.lower()
    assert all(internal not in text for internal in ("baseline", "units", "mql5_web", "T12:30:00Z"))
    assert "https://" not in text


def test_dry_run_message_and_snapshot_do_not_claim_live_delivery():
    event = fixture("macro.release")
    text = MessageHTML(render(event, {}, dry_run=True)).visible.lower()
    snapshot = "\n".join(MessageHTML(page).visible.lower() for page in render_snapshot_messages([fixture("macro", observation=True)], dry_run=True))
    for message in (text, snapshot):
        assert any(marker in message for marker in ("тест", "replay", "синтетичес", "dry-run"))
        assert "live-алерты включены" not in message


def macro_snapshot(series: str, *, label=None, actual="3.1", forecast="3.0", previous="2.9", scheduled="2026-10-29T12:30:00Z") -> dict:
    item = fixture("macro", observation=True)
    item["subject"].update(series_id=series, label=label or series)
    item["values"].update(actual=actual, forecast=forecast, previous=previous,
                          publication_status="scheduled" if actual is None else "published",
                          scheduled_at=scheduled, published_at=None if actual is None else scheduled)
    return item


def snapshot_views(items: list[dict]) -> list[MessageHTML]:
    return [MessageHTML(page) for page in render_snapshot_messages(items, at="2026-10-30T11:00:00Z")]


def test_snapshot_keeps_more_than_three_published_series_and_both_rate_conventions():
    series = ["US.PCE.HEADLINE.YOY", "US.PCE.HEADLINE.MOM", "US.PCE.CORE.YOY", "US.PCE.CORE.MOM",
              "US.GDP.QOQ.ANNUALIZED", "US.FED.TARGET.UPPER", "US.FED.TARGET.MIDPOINT"]
    items = [macro_snapshot(key, actual=str(51 + index)) for index, key in enumerate(series)]
    visible = "\n".join(page.visible for page in snapshot_views(items))
    assert all(f"{51 + index}%" in visible for index in range(len(series)))
    assert "середина диапазона" in visible and "верхняя граница" in visible


def test_upcoming_forecast_is_separate_and_cannot_hide_the_last_actual():
    published = macro_snapshot("US.CPI.HEADLINE.YOY", actual="3.1")
    upcoming = macro_snapshot("US.CPI.HEADLINE.YOY", actual=None, forecast="4.2", previous="3.7", scheduled="2026-11-10T13:30:00Z")
    upcoming["subject"].update(reference_period="unknown", release_key="next-cpi")
    pages = snapshot_views([upcoming, published])
    actual_page = next(page.visible for page in pages if "3,1%" in page.visible)
    calendar_page = next(page.visible for page in pages if "4,2%" in page.visible)
    assert actual_page != calendar_page
    assert "3,7%" in calendar_page
    assert re.search(r"[Пп]рогноз[^\n]*4,2%", calendar_page)
    assert not re.search(r"[Фф]акт\s*:\s*(?:3,7|4,2)%", calendar_page)
    assert "10.11.2026" in calendar_page


def test_zero_forecast_is_shown_but_cancelled_release_is_excluded():
    upcoming = macro_snapshot("US.ECI.QOQ", actual=None, forecast="0", previous="0.9", scheduled="2026-10-30T12:30:00Z")
    cancelled = macro_snapshot("CUSTOM.CANCELLED", label="Отменённый показатель", actual=None, forecast="77")
    cancelled["values"]["publication_status"] = "cancelled"
    visible = "\n".join(page.visible for page in snapshot_views([upcoming, cancelled]))
    assert re.search(r"[Пп]рогноз[^\n]*0%", visible)
    assert "0,9%" in visible
    assert "Отменённый показатель" not in visible and "77%" not in visible


def test_duplicate_sources_do_not_mix_values_or_create_two_rows():
    sparse = macro_snapshot("CUSTOM.PROVENANCE", label="Проверка происхождения", actual="3", forecast=None, previous=None)
    sparse["source"].update(provider_id="xoomar", source_url="https://xoomar.com/sparse")
    complete = macro_snapshot("CUSTOM.PROVENANCE", label="Проверка происхождения", actual="2", forecast="5", previous="11.4")
    complete["subject"]["reference_period"] = "unknown"
    complete["source"].update(provider_id="mql5_web", source_url="https://www.mql5.com/complete")
    pages = snapshot_views([sparse, complete])
    visible = "\n".join(page.visible for page in pages)
    links = [link for page in pages for link in page.links]
    assert visible.count("Проверка происхождения") == 1
    assert "2%" in visible and "5%" in visible and "11,4%" in visible
    assert "3%" not in visible
    assert complete["source"]["source_url"] in links
    assert sparse["source"]["source_url"] not in links


def test_snapshot_scenario_shows_selected_outcome_subpercent_quote_and_sample_time():
    item = fixture("scenario", observation=True)
    item["source"]["provider_id"] = "polymarket"
    item["subject"].update(outcome_label="No", label='Сценарий <&> "тест"')
    item["values"].update(probability="0.0065", sample_at="2026-10-30T10:34:00Z")
    item["observed_at"] = "2026-10-30T11:00:00Z"
    pages = snapshot_views([item])
    visible = "\n".join(page.visible for page in pages)
    assert "0,65%" in visible and "Нет" in visible
    assert "11:34" in visible and "CET" in visible
    assert 'Сценарий <&> "тест"' in visible
    assert item["source"]["source_url"] in [link for page in pages for link in page.links]
    assert item["subject"]["outcome_id"] not in visible


def test_many_rows_paginate_without_losing_rows_or_cutting_html():
    items = []
    for index in range(24):
        item = macro_snapshot(f"CUSTOM.SERIES.{index}", label=f"Индикатор {index:02d} " + "🔥" * 65,
                              actual=str(100 + index))
        item["source"]["source_url"] = f"https://example.com/calendar/{index}?a=1&b=%22test%22"
        items.append(item)
    messages = render_snapshot_messages(items, at="2026-10-30T11:00:00Z")
    assert len(messages) > 1
    assert all(len(message.encode("utf-16-le")) // 2 <= 4096 for message in messages)
    pages = [MessageHTML(message) for message in messages]
    visible = "\n".join(page.visible for page in pages)
    for index in range(24):
        assert visible.count(f"Индикатор {index:02d}") == 1
        assert f"{100 + index}%" in visible
    assert sorted(link for page in pages for link in page.links) == sorted(item["source"]["source_url"] for item in items)
    assert messages == render_snapshot_messages(list(reversed(items)), at="2026-10-30T11:00:00Z")

