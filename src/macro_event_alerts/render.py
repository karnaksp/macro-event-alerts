"""Compact Russian Telegram HTML; dynamic text is escaped before adding markup."""
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .domain import decimal_text, instant

LOCAL_ZONE = ZoneInfo("Europe/Podgorica")
SERIES_LABELS = {
    "US.CPI.HEADLINE.YOY": "Инфляция CPI США · год к году",
    "US.CPI.HEADLINE.MOM": "Инфляция CPI США · месяц к месяцу",
    "US.CPI.CORE.YOY": "Базовая инфляция CPI США · год к году",
    "US.CPI.CORE.MOM": "Базовая инфляция CPI США · месяц к месяцу",
    "US.PCE.HEADLINE.YOY": "Инфляция PCE США · год к году",
    "US.PCE.HEADLINE.MOM": "Инфляция PCE США · месяц к месяцу",
    "US.PCE.CORE.YOY": "Базовая инфляция PCE США · год к году",
    "US.PCE.CORE.MOM": "Базовая инфляция PCE США · месяц к месяцу",
    "US.GDP.QOQ.ANNUALIZED": "ВВП США · кв/кв, годовой темп",
    "US.NFP.CHANGE": "Изменение занятости в США · NFP",
    "US.UNEMPLOYMENT.RATE": "Безработица в США",
    "US.ECI.QOQ": "Стоимость труда в США (ECI) · кв/кв",
    "US.FED.TARGET.MIDPOINT": "Ставка ФРС · середина диапазона",
    "US.FED.TARGET.UPPER": "Ставка ФРС · верхняя граница",
}
MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
FORECAST_LABELS = {"consensus": "Консенсус", "model": "Модельный прогноз", "official_projection": "Официальный прогноз", "unknown": "Прогноз источника"}


def text(value, limit=180) -> str:
    value = " ".join(str(value).split())
    return escape(value if len(value) <= limit else value[:limit-1] + "…")


def number(value) -> str:
    return decimal_text(Decimal(value)).replace(".", ",")


def value_text(value, subject: dict, *, difference=False) -> str:
    if value is None:
        return "нет данных"
    unit = subject["unit"]
    suffix = "п.п." if difference and unit == "percent" else {
        "percent": "%", "persons": "чел.", "usd": "$", "index": "пунктов", "other": "ед.",
    }[unit]
    scale = {"units": "", "thousands": "тыс. ", "millions": "млн ", "billions": "млрд "}[subject["scale"]]
    separator = "" if suffix == "%" and not scale else " "
    return number(value) + separator + scale + suffix


def period_text(value: str) -> str:
    if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        year, month = value.split("-")
        return f"{MONTHS[int(month)-1]} {year}"
    if re.fullmatch(r"\d{4}-Q[1-4]", value):
        return f"{value[-1]}-й квартал {value[:4]}"
    return text(value, 64)


def date_text(value: str | datetime) -> str:
    date = instant(value) if isinstance(value, str) else value
    local = date.astimezone(LOCAL_ZONE)
    return local.strftime("%d.%m.%Y · %H:%M %Z")


def source_link(source: dict) -> str:
    url = source["source_url"]
    label = {"mql5_web": "MQL5", "xoomar": "XOOMAR", "polymarket": "Polymarket"}.get(source["provider_id"])
    label = label or urlsplit(url).hostname or "Источник"
    return f'<a href="{escape(url, quote=True)}">{text(label, 60)}</a>'


def macro_label(subject: dict) -> str:
    return text(SERIES_LABELS.get(subject["series_id"], subject["label"]))


def macro_context(subject: dict, payload: dict) -> list[str]:
    period = subject["reference_period"]
    lines = ["Период: " + (period_text(period) if period != "unknown" else "не указан источником")]
    if payload.get("published_at"):
        lines.append("Опубликовано: " + date_text(payload["published_at"]))
    elif payload.get("scheduled_at"):
        lines.append("По календарю: " + date_text(payload["scheduled_at"]))
    return lines


def render(event: dict, context: dict, *, dry_run: bool = True) -> str:
    subject, payload, source = event["subject"], event["payload"], event["source"]
    event_type = event["event_type"]
    lines = ["🧪 <b>Тест · синтетические данные</b>", ""] if dry_run else []
    if subject["kind"] == "macro":
        headline = {"macro.release": "📊 Вышла статистика", "macro.forecast_changed": "📝 Обновился прогноз", "macro.revised": "🔄 Пересмотр статистики"}[event_type]
        lines.extend([f"<b>{headline}</b>", macro_label(subject), ""])
        if event_type == "macro.release":
            lines.append(f"Факт: <b>{value_text(payload['actual'], subject)}</b>")
            forecast_label = FORECAST_LABELS[payload["forecast_origin"]]
            lines.append(f"{forecast_label}: {value_text(payload['forecast'], subject)}")
            previous_period = payload.get("previous_reference_period")
            previous_label = "Предыдущее" + (f" ({period_text(previous_period)})" if previous_period and previous_period != "unknown" else "")
            lines.append(f"{previous_label}: {value_text(payload['previous'], subject)}")
            if payload["actual"] is not None and payload["forecast"] is not None:
                delta = Decimal(payload["actual"]) - Decimal(payload["forecast"])
                comparison = "Выше прогноза на " if delta > 0 else "Ниже прогноза на "
                lines.append("Совпало с прогнозом" if delta == 0 else comparison + value_text(abs(delta), subject, difference=True))
        else:
            before, after = (payload["before"], payload["after"]) if event_type == "macro.forecast_changed" else (payload["before_actual"], payload["after_actual"])
            lines.append(f"{value_text(before, subject)} → <b>{value_text(after, subject)}</b>")
            if event_type == "macro.forecast_changed":
                lines.append(FORECAST_LABELS[payload["forecast_origin"]])
        lines.extend(["", *macro_context(subject, payload)])
    else:
        label = subject["label"]
        before = number(Decimal(context["before"]) * 100)
        after = number(Decimal(context["after"]) * 100)
        delta = Decimal(context["delta_pp"])
        change = ("+" if delta > 0 else "") + number(delta)
        duration = ""
        if context.get("window_start") and context.get("window_end"):
            minutes = Decimal(str((instant(context["window_end"]) - instant(context["window_start"])).total_seconds())) / 60
            duration = " за " + number(minutes) + " мин"
        basis = {"midpoint": "Середина спреда", "last_trade": "Последняя сделка", "provider_probability": "Оценка источника"}[subject["price_basis"]]
        outcome = subject.get("outcome_label") or subject["outcome_id"]
        outcome = {"yes": "Да", "no": "Нет"}.get(outcome.casefold(), outcome)
        lines.extend(["<b>🎯 Изменилась рыночная оценка</b>", text(label), "", f"Цена исхода: {before}% → <b>{after}%</b>",
                      f"Изменение: <b>{change} п.п.</b>{duration}", "Исход: " + text(outcome, 100), "", "Срок сценария: " + date_text(subject["horizon_at"]), basis])
    lines.extend(["Получено: " + date_text(context.get("current_observed_at", event["observed_at"])), "Источник: " + source_link(source)])
    if source.get("declared_delay_seconds") is not None:
        lines.append(f"Заявленная задержка источника: {source['declared_delay_seconds']} с")
    return "\n".join(lines)


def render_snapshot(observations: list[dict], *, at=None, dry_run=False) -> str:
    """Manual connection check with up to three saved results, never a release alert."""
    lines = ["✅ <b>Подключение проверено</b>", "Бот может отправлять сообщения в этот чат.", "", "<b>Последние значения из календаря</b>"]
    if dry_run:
        lines = ["🧪 <b>Тест · синтетические данные</b>", "", "<b>Сохранённые значения</b>"]
    published = [item for item in observations if item["subject"]["kind"] == "macro" and item["values"]["actual"] is not None]
    published.sort(key=lambda item: (item["values"].get("scheduled_at") or "", item["subject"]["reference_period"] != "unknown", item["observed_at"]), reverse=True)
    selected, seen = [], set()
    for item in published:
        series = item["subject"]["series_id"]
        if series not in seen:
            selected.append(item)
            seen.add(series)
        if len(selected) == 3:
            break
    for item in selected:
        subject, values = item["subject"], item["values"]
        lines.extend(["", macro_label(subject), f"<b>{value_text(values['actual'], subject)}</b> · {source_link(item['source'])}"])
        if subject["reference_period"] != "unknown":
            lines.append("Период: " + period_text(subject["reference_period"]))
        release_time = values.get("published_at") or values.get("scheduled_at")
        if release_time:
            lines.append(("Опубликовано: " if values.get("published_at") else "По календарю: ") + date_text(release_time))
    if not selected:
        lines.extend(["", "Опубликованных значений пока нет."])
    if any(item["subject"]["reference_period"] == "unknown" for item in selected):
        lines.extend(["", "Отчётные периоды не указаны источником для части значений."])
    lines.extend(["", "<i>Контрольный снимок сохранённых данных, не новый релиз.</i>", "Снимок на " + date_text(at or datetime.now(timezone.utc))])
    return "\n".join(lines)
