# Macro Event Alerts

[![Contracts](https://github.com/karnaksp/macro-event-alerts/actions/workflows/contracts.yml/badge.svg)](https://github.com/karnaksp/macro-event-alerts/actions/workflows/contracts.yml)
[![Runtime](https://github.com/karnaksp/macro-event-alerts/actions/workflows/runtime.yml/badge.svg)](https://github.com/karnaksp/macro-event-alerts/actions/workflows/runtime.yml)

Самостоятельно размещаемый сервис алертов по макростатистике и рыночным оценкам сценариев в Telegram. Open source, Apache 2.0.

**P1 реализован:** offline replay → проверка контрактов → SQLite → события → правила → сохранённая очередь → dry-run. Docker собирается и запускается. Все демо-данные синтетические. Live API-адаптеры и отправка сообщений в Telegram относятся к следующим этапам и ещё не реализованы.

## Запустить Docker

Нужны Docker Engine с Compose. Токен бота и API-ключи для P1 не требуются.

```bash
docker compose up --build -d
docker compose logs replay
```

Первый запуск обрабатывает 12 наблюдений, сохраняет 7 доменных событий и 6 сообщений dry-run. Сервис остаётся запущенным, проверяет fixture каждые 30 секунд и продолжает с сохранённого checkpoint. Повторный запуск не создаёт новые события или сообщения.

```bash
docker compose run --rm replay status --state /data/state.db
docker compose run --rm replay health --state /data/state.db
docker compose stop
```

Compose сохраняет SQLite в именованном volume. `stop` и `down` сохраняют данные; удаление volume удаляет историю, checkpoint и очередь. Одновременно один worker может владеть этим state path; `status`/`health` читают его без блокировки worker.

Для одноразового replay остановите фоновый worker, затем выполните:

```bash
docker compose run --rm replay replay --config /app/config.replay.yaml --fixture /app/examples/replay/demo.jsonl
```

`dry_run` означает подготовку сообщения с записью в очередь и выводом JSON в stdout; это отдельный статус, не подтверждение доставки Telegram. Контейнер P1 работает без сети, без root, с read-only filesystem и writable `/data`. `.env` исключён из build context и не передаётся в контейнер.

## Какие события работают в replay

| Событие | Поведение |
| --- | --- |
| `macro.release` | Первое actual известного выпуска; факт, прогноз и previous хранятся раздельно |
| `macro.forecast_changed` | Сравнение числовых прогнозов одного источника/периода/origin до actual |
| `macro.revised` | Пересмотр actual того же опубликованного выпуска |
| `scenario.probability_changed` | Переход рыночной цены конкретного исхода; правила оценивают изменение за окно |

Холодный старт сохраняет baseline без рассылки истории. `null` не равен нулю. Смена периода, источника, единиц, origin прогноза или resolution criteria не выдаётся за пересмотр того же значения. Пересмотр прошлого периода из поля `previous` требует однозначного mapping адаптера; P1 сохраняет это поле как контекст и не выдумывает release identity.

При 55 → 64 → 75% последнее событие содержит +11 п.п., а сообщение за час — +20 п.п. из сохранённого evaluation context. Свежая неизменившаяся цена тоже проверяет завершившееся окно. Правила фильтруют серии/контракты, возраст, спред, ликвидность, порог и cooldown; причины подавления видны в summary. Прогноз consensus, модельная оценка и цена рынка остаются разными сущностями.

## Локальная разработка

Установите [uv](https://docs.astral.sh/uv/getting-started/installation/). Затем:

```bash
uv sync --locked
uv run macro-event-alerts validate-config --config config.replay.yaml
uv run macro-event-alerts replay --config config.replay.yaml --fixture examples/replay/demo.jsonl --state data/state.db
uv run macro-event-alerts status --state data/state.db
uv run pytest -q
uv run python scripts/validate_contracts.py
```

Replay использует время из наблюдений как логические часы: он не маскирует исторический fixture под свежие данные. `--stop-after N` останавливает чтение после N новых строк; `--no-drain` сохраняет pending outbox для проверки восстановления. Изменение содержимого fixture создаёт новый cursor namespace, но сохранённые observation ID и domain events не повторяются.

## Конфигурация и дальнейшие источники

[`config.replay.yaml`](config.replay.yaml) — рабочая конфигурация P1 с fixture adapter. [`config.example.yaml`](config.example.yaml) — проектный пример будущих API; P1 отклоняет не реализованные live adapters и `dry_run:false`. Включение правила проверяет capabilities доступного адаптера.

`.env.example` задаёт имена Telegram-переменных для будущего sender. Реальные значения остаются в локальном `.env`; P1 их не читает. Поддержка live-провайдеров, повторов Telegram и external delivery будет добавляться по [backlog P2–P6](https://github.com/karnaksp/macro-event-alerts/issues?q=is%3Aissue%20is%3Aopen%20sort%3Acreated-asc).

Бесплатные API дают разные возможности и задержки; быстрый бесплатный consensus не обещается. MQL5 web остаётся экспериментальным, MT5 требует отдельного bridge, CME EOD и intraday — разные продукты. Доступ к данным и перепубликация регулируются поставщиком; Apache 2.0 относится к коду.

## Документация

- [Архитектура и транзакции](docs/architecture.md)
- [Проверка P1 и границы готовности](docs/p1-verification.md)
- [Источники, доступ, задержки](docs/sources.md)
- [Контракт наблюдений/событий](.archcore/events.spec.md), [адаптеров/правил](.archcore/providers-and-rules.spec.md), [Telegram](.archcore/telegram-delivery.spec.md)
- [Требования](.archcore/product.prd.md), [план реализации](.archcore/implementation.plan.md), [приёмочные сценарии](docs/acceptance-cases.md)
- [JSON Schema v1](contracts/v1/) и [синтетические примеры](examples/)

Нормативные документы остаются draft; статусы реализации указаны отдельно. Для чтения документации Archcore устанавливать не требуется.
