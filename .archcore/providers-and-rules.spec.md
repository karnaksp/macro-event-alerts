---
title: "Адаптеры источников и правила подписки"
status: draft
tags:
  - "macro-alerts"
---

## Purpose & Scope

Контракт adapter → ingestion и event → subscription. От него зависят scheduler, конфигурация и подключение нового поставщика без изменения правил.

## Surface

- `@config.example.yaml`, `@contracts/v1/config.schema.json`: конфигурация и правила.
- Adapter surface: capabilities, fetch(checkpoint, budget), FetchBatch(raw, observations, next_checkpoint, next_poll_at); P1 fixture ingestion: `@src/macro_event_alerts/app.py`; live adapters: `@src/macro_event_alerts/providers.py`, `@src/macro_event_alerts/polymarket.py`.
- Capabilities: actual, forecast, revisions, scenario_probability; forecast origins, транспорт, задержка, авторизация, лимиты и статус проверки.
- Source-specific mapping: source series → canonical series, timezone, scale, release identity; подтверждается raw fixtures.

## Normative Behavior

1. WHEN scheduler опрашивает источник, scheduler MUST применять общий бюджет ко всем запросам, повторам, страницам и metadata.
2. WHEN адаптер возвращает FetchBatch, ingestion MUST продвинуть checkpoint только после commit принятых наблюдений.
3. WHEN активное правило требует отсутствующую capability, config validator MUST отклонить конфигурацию с provider/rule ID и причиной.
4. WHEN правило обрабатывает событие, rules engine MUST сопоставить provider, event type и указанный series_id либо contract_id.
5. WHEN событие превышает допустимую давность, rules engine MUST подавить уведомление с диагностируемой причиной.
6. WHEN правило сравнивает вероятность, rules engine MUST использовать последний принятый quality-passing снимок до начала comparison window.
7. WHEN изменение достигает порога вне cooldown, rules engine MUST сохранить задание с неизменяемым evaluation context.
8. WHEN наступает следующий успешный comparison window, rules engine MUST заново оценить изменение по его опорному снимку.
9. WHEN провайдер недоступен, scheduler MUST продолжить работу других включённых источников.
10. WHEN источник требует платный ключ, adapter MUST включаться только явной конфигурацией с ключом.
11. WHEN свежий scenario snapshot завершает окно без нового перехода, rules engine MUST оценить окно относительно последнего сохранённого change event.

## Constraints & Invariants

- Config validator MUST отклонять неизвестные поля и повторяющиеся rule ID.
- Scheduler MUST применять timeout и backoff к каждому источнику независимо.
- Adapter MUST сохранять forecast_origin; одинаковое имя forecast не подтверждает consensus.
- MQL5 web MUST оставаться experimental и opt-in; MT5 bridge требует отдельного транспорта.
- Rules engine MUST учитывать возраст исходного sample для scenario, а не только observed_at.
- Rules engine MUST подавлять scenario при неизвестной ликвидности, если min_liquidity_usd задан.
- Rules engine MUST подавлять midpoint без двух сторон книги либо при spread выше max_spread_pp.
- Rules engine MUST исключать placeholder, stale last trade, closed и resolved рынки из live repricing.
- Порог в п.п. не переносится на макроэкономические величины; для macro.release фильтруются факт публикации и выбранная серия.
- Evaluation context: rule version, опорный/current observation ID и времена, window_start/end, before/after, delta_pp; окно отображается из context.
- Rules engine MUST применять cooldown_seconds отдельно для rule и назначения; повторная доставка использует исходный evaluation context.
- Window применяется к scenario rule; forecast и revision сравнивают последовательные принятые значения одного выпуска.
- [assumption] При первом включении правила rules engine MUST создать baseline окна без исторической рассылки.

## Failure Behavior

1. IF опорный снимок comparison window отсутствует, THEN rules engine MUST сохранить baseline без уведомления об изменении.
2. IF источник исчерпал бюджет, THEN scheduler MUST отложить запрос до пополнения лимита без подмены поставщика.
3. IF единицы неизвестны либо отсутствуют период и стабильный release ID, THEN adapter MUST вернуть ошибку данных.
4. IF API не публикует forecast, THEN adapter MUST сохранить null и выключенную forecast capability.
5. IF критерии разрешения сценария меняются, THEN adapter MUST начать новый baseline с новым resolution_hash.
6. IF правило содержит неразрешённый market placeholder, THEN config validator MUST отклонить включение правила.

## Conformance

Конфигурация и правила реализованы в `@src/macro_event_alerts/config.py` и `@src/macro_event_alerts/engine.py`; форма проверяется `@scripts/validate_contracts.py`; полное соответствие требует capability fixtures, budget tests и runtime-сценариев `@docs/acceptance-cases.md`.
Измерение задержки поставщика отделено от времени обработки приложения; отсутствие source timestamp не позволяет заявить измеренный release lag.

MQL5 listing MAY использовать reference_period=unknown только с vendor release ID; дата релиза не заменяет период статистики.
MQL5 расширен сериями PPI/Core PPI MoM/YoY, retail MoM/ex-autos MoM, initial jobless claims и ISM manufacturing/services PMI. Claims хранят количество людей, PMI — уровень индекса; процентные изменения не подменяют эти единицы. Source mapping и первичные ссылки находятся в @docs/sources.md, правила — в @config.live.yaml.
Публичные API проверены реальными read-only fetch; платные и trading endpoints не используются.
