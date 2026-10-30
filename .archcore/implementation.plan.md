---
title: "План реализации Macro Event Alerts"
status: draft
tags:
  - "macro-alerts"
---

## Goal

Довести опубликованные контракты до первого запускаемого Docker-релиза с проверенными API-адаптерами и Telegram-доставкой. Этот репозиторий пока фиксирует дизайн; завершение планирования не означает готовность приложения.

## Declared Delta

- create — требования нового сервиса: product.prd.md.
- create — контракт наблюдений и событий: events.spec.md и JSON Schema.
- create — контракт адаптеров и подписок: providers-and-rules.spec.md и пример конфигурации.
- create — контракт outbox и Telegram: telegram-delivery.spec.md.
- create — фазовый backlog реализации, критерии приёмки и ограничения источников.

## Tasks

### P0 — План и контракты (подготовлены)

- [x] Зафиксировать scope и границы готовности: `@README.md`, `@docs/architecture.md`.
- [x] Сохранить матрицу доступа и ограничений: `@docs/sources.md`.
- [x] Подготовить JSON Schema и синтетические fixtures: `@contracts/v1/`, `@examples/`.
- [x] Добавить конфигурационный пример и локальную/CI проверку: `@config.example.yaml`, `@scripts/validate_contracts.py`, `@.github/workflows/contracts.yml`.
- [x] Создать личный публичный репозиторий и issues P1–P6; факт публикации проверяется отдельно от runtime.

### P1 — Сквозной replay и Docker

- [ ] Создать worker, fixture adapter, SQLite migrations, detector, rules engine и dry-run sink; runtime-цель не найдена, файлов приложения пока нет.
- [ ] Добавить Dockerfile и compose с запускаемым replay, non-root и volume; файлов сборки пока нет, выбрать их при реализации.
- [ ] Связать replay fixtures с `@examples/`, сценариями `@docs/acceptance-cases.md` и текущими схемами `@contracts/v1/`.

### P2 — Бесплатные результаты макростатистики

- [ ] Провести короткую проверку XOOMAR для 2–3 серий, сохранить raw fixture и mapping; кода адаптера пока нет.
- [ ] Реализовать общий request budget, scheduler, freshness и provider checkpoint; runtime-цель не найдена.
- [ ] Добавить FXMacroData USD как дополнительный adapter с подтверждённой задержкой/квотой из `@docs/sources.md`; runtime-цель не найдена.
- [ ] Расширить охват только после проверки каждой серии; ECI оставить до подтверждённого actual fixture.

### P3 — Обновления прогнозов

- [ ] Проверить экспериментальный MQL5 web endpoint и timezone, затем реализовать opt-in adapter; runtime-цель не найдена.
- [ ] Сохранить forecast snapshots, origin и before/after fixtures по `@contracts/v1/observation.schema.json`.
- [ ] Официальный MT5 bridge оценить отдельной задачей; не добавлять скрытую зависимость Windows в Linux Docker MVP.

### P4 — Изменения рыночных сценариев

- [ ] Выбрать конкретный Polymarket outcome и сохранить resolution criteria; live ID перед подключением задаёт пользователь.
- [ ] Реализовать public metadata/price adapter и фильтры качества/окна; runtime-цель не найдена.
- [ ] Проверить placeholder, stale last trade и пустой book по `@docs/acceptance-cases.md`.
- [ ] Kalshi и CME оставить расширениями после первого релиза; доступ/лицензию CME проверить отдельно.

### P5 — Telegram sender и эксплуатация

- [ ] Реализовать renderer, durable outbox, retry и dead_letter, используя `@.env.example`; runtime-цель не найдена.
- [ ] Проверить Telegram failures fake transport, crash/restart и миграции; runtime/test-цели не найдены.
- [ ] Добавить health, source freshness, outbox age и redacted logs; runtime-цель не найдена.
- [ ] Подготовить реальные Docker-команды и инструкцию подключения после прохождения replay.

### P6 — Первый релиз и подключение

- [ ] Сверить фактические capability каждого включаемого адаптера и обновить `@docs/sources.md`.
- [ ] Провести установку из чистого clone, Docker replay и перезапуск volume.
- [ ] Подключить заданные пользователем Telegram chat/topic и API credentials; отправить только явно разрешённый smoke test.
- [ ] Выпустить tag и Docker image после сверки implementation с контрактами; repository/image registry выбрать при реализации.

## Acceptance Criteria

| Этап | Проверяемое доказательство закрытия |
| --- | --- |
| P0 | Ссылка GitHub, 6 issues, passing validator и workflow с доступным логом |
| P1 | Записанные Docker replay command/result; состояние SQLite до/после рестарта; список принятых event ID |
| P2 | Raw fixtures, mapping серий, capability report и журнал расхода общего request budget |
| P3 | Fixture последовательности forecast snapshots с origin, периодом и временными метками |
| P4 | Зафиксированный market/outcome и resolution hash; отчёт quality/window проверок |
| P5 | Fake transport transcript с 429/5xx/ok:false/timeout/401/403; журнал восстановления outbox |
| P6 | Чистая установка, release tag, digest образа, перечень поддержанных источников и результат разрешённого smoke test |

P1–P6 пока не выполнены. Документы draft проходят принятие после рассмотрения владельцем; plan закрывается после проверки всех задач.

## Dependencies

P1 зависит от контрактов P0; P2, P3, P4 зависят от P1 и могут разрабатываться параллельно. P5 зависит от core/outbox P1 и не требует готовности всех адаптеров. P6 зависит от P2/P4/P5; P3 входит в первый релиз только если экспериментальная интеграция прошла проверки, иначе capability forecast явно выключена.

Перед live подключением нужны Telegram token/chat ID и выбранный scenario contract. Доступность MQL5 web, покрытие серий, условия API и фактическая задержка остаются внешними ограничениями. Автоматическая покупка данных не входит в план.
