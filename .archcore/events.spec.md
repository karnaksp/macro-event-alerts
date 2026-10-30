---
title: "Наблюдения и канонические события"
status: draft
tags:
  - "macro-alerts"
---

## Purpose & Scope

Контракт нормализованных наблюдений и переходов состояния для адаптеров, detector, правил и renderer. Форма проверяется схемами; P1 runtime выполняет offline replay.

## Surface

- `@contracts/v1/common.schema.json`: числовые значения, время, provenance и субъекты.
- `@contracts/v1/observation.schema.json`: вход adapter → ingestion.
- `@contracts/v1/event.schema.json`: выход detector → rules/outbox.
- `@src/macro_event_alerts/domain.py`, `@src/macro_event_alerts/engine.py`: identity и переходы.
- `@examples/events/`, `@examples/observations/`, `@examples/replay/`: синтетические примеры.
- JSON Schema IDs — стабильные идентификаторы; resolver использует локальный registry, публикация GitHub Pages не требуется.

## Normative Behavior

1. WHEN адаптер получает ответ, адаптер MUST сохранить provenance и нормализовать наблюдение по входной схеме.
2. WHEN detector впервые видит субъект, detector MUST сохранить baseline без доменного алерта.
3. WHEN известный live выпуск впервые получает actual, detector MUST создать `macro.release` независимо от равенства прошлому периоду.
4. WHEN два числовых дорелизных прогноза одного origin различаются, detector MUST создать `macro.forecast_changed`.
5. WHEN actual опубликован, detector MUST прекратить дорелизные алерты прогноза этого выпуска.
6. WHEN принятый опубликованный actual меняется, detector MUST создать `macro.revised` для пересматриваемого периода.
7. WHEN previous меняется без однозначного прошлого периода, detector MUST сохранить изменение только как контекст текущего выпуска.
8. WHEN сопоставимая цена сценария меняется, detector MUST создать `scenario.probability_changed` с `delta_pp = (after − before) × 100`.
9. WHEN принят переход значений, detector MUST увеличить сохранённую `state_version` субъекта.
10. WHEN ingestion фиксирует переход, ingestion MUST атомарно сохранить наблюдение, baseline, событие, outbox и checkpoint.
11. WHEN повторный ответ не меняет состояние, detector MUST сохранить прежнюю версию без нового события.
12. WHEN источник меняет revision монотонно, ingestion MUST использовать её порядок вместо порядка получения ответов.
13. WHEN source timestamp отсутствует, detector MUST обозначить время публикации неизвестным.

## Constraints & Invariants

- Ingestion MUST представлять числа Decimal-строками: отсутствие `null` отличается от нуля.
- Detector MUST вычислять SHA-256 canonical JSON `subject_key` по provider ID и полям идентичности ниже.
- Macro identity: kind, country, currency, series_id, release_key, reference_period, frequency, adjustment, unit, scale, transformation.
- Scenario identity: kind, contract_id, outcome_id, horizon_at, resolution_hash, price_basis.
- Canonical JSON: UTF-8, ключи по алфавиту, без пробелов, `ensure_ascii=false`; объект `{provider_id, subject:{identity fields}}`.
- Detector MUST сравнивать forecast только при одинаковом forecast_origin; новый origin создаёт отдельный baseline прогноза.
- Ingestion MUST хранить нормализованное время в UTC `Z`, отдельно от времени опроса и торгового сервера.
- Detector MUST сохранять event_id перехода: одинаковый hash значения не объединяет переходы A→B→A.
- Ingestion MUST хранить исходный raw ответ и проверяемый `raw_sha256` до его удаления по retention.
- Detector MUST отличать published revision от исторического backfill; backfill не участвует в live baseline.
- Probability MUST лежать в [0,1]; это цена/оценка конкретного исхода, а не доля опрошенных трейдеров.
- `published_at`, `source_updated_at`, `declared_delay_seconds` nullable; неизвестная задержка не равна нулевой.

## Failure Behavior

1. IF наблюдение нарушает схему, THEN ingestion MUST поместить его в карантин без изменения baseline.
2. IF прогноз меняется с null на число, THEN detector MUST инициализировать baseline прогноза без события изменения.
3. IF снимок старше принятой source revision или timestamp, THEN ingestion MUST исключить его из live переходов.
4. IF actual отсутствует, THEN detector MUST исключить событие `macro.release`.
5. IF snapshot меняет единицы или идентичность субъекта, THEN detector MUST начать отдельный baseline.
6. IF источник недоступен, THEN ingestion MUST сохранить прежнее состояние без замены значений на null.
7. IF порядок источника неизвестен, THEN ingestion MUST сравнивать только последовательные live запросы, исключая backfill.

## Conformance

Соответствие требует всех MUST, схем и инвариантов; форма проверяется `@scripts/validate_contracts.py`, переходы runtime проверяются по `@docs/acceptance-cases.md`.
P1 проверяет baseline, порядок, транзакции и crash/restart через `@tests/`; live-provider mappings остаются последующими этапами.
Ingestion MUST отклонять source_updated_at и published_at позже observed_at: будущая дата не становится baseline.
Ingestion MUST отклонять actual и published_at для scheduled/cancelled наблюдений.
