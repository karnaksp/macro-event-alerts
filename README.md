# Macro Event Alerts

[![Contracts](https://github.com/karnaksp/macro-event-alerts/actions/workflows/contracts.yml/badge.svg)](https://github.com/karnaksp/macro-event-alerts/actions/workflows/contracts.yml)

Самостоятельно размещаемый сервис алертов по макростатистике и рыночным оценкам сценариев в указанный чат Telegram. Open source, Apache 2.0.

**Статус: проектирование и контракты.** В репозитории есть план, схемы данных, синтетические примеры и проверка контрактов. Worker, адаптеры, отправка в Telegram и Docker-образ ещё не реализованы.

## Что будет отправлять

| Событие | Пример содержания |
| --- | --- |
| `macro.release` | CPI опубликован: факт 3.1%, прогноз 3.0%, предыдущее 2.9% |
| `macro.forecast_changed` | Прогноз CPI того же источника изменился с 2.9% на 3.0% до релиза |
| `macro.revised` | Опубликованный результат за прошлый период пересмотрен с 3.1% до 3.2% |
| `scenario.probability_changed` | Цена определённого исхода выросла с 55% до 75%: +20 п.п. |

Числа выше синтетические. У каждого сообщения будут источник, ссылка, период/срок сценария, единицы и время. Прогноз consensus, модельный прогноз и цена prediction market обозначаются отдельно.

## Устройство

API → адаптер → нормализованное наблюдение → сравнение снимков → событие → YAML-правило → SQLite outbox → Telegram.

Предлагаемый MVP: один Python worker в Docker, SQLite в постоянном volume, Telegram chat ID и опциональный topic ID через окружение. Наблюдения и очередь сохраняются при перезапуске. Пользователь выбирает источники, события, серии, сценарии, пороги и допустимую давность в YAML. Новый API подключается отдельным адаптером к общему контракту.

## План и контракты

- [Требования продукта](.archcore/product.prd.md)
- [Архитектура и хранение состояния](docs/architecture.md)
- [Матрица источников: доступ, задержки, возможности](docs/sources.md)
- [Контракт наблюдений и событий](.archcore/events.spec.md)
- [Контракт адаптеров и правил](.archcore/providers-and-rules.spec.md)
- [Контракт доставки Telegram](.archcore/telegram-delivery.spec.md)
- [Этапы реализации и критерии готовности](.archcore/implementation.plan.md)
- [Backlog: P1–P6 в GitHub Issues](https://github.com/karnaksp/macro-event-alerts/issues?q=is%3Aissue%20is%3Aopen%20sort%3Acreated-asc)
- [Приёмочные сценарии runtime](docs/acceptance-cases.md)
- [JSON Schema v1](contracts/v1/) · [примеры событий](examples/events/) · [пример конфигурации](config.example.yaml)

Документы `.archcore/` имеют статус draft: это исходный план для обсуждения и реализации. Для чтения Archcore устанавливать не требуется.

## Первый набор источников

Бесплатный macro adapter начнётся с XOOMAR после проверки покрытия; FXMacroData — дополнительный источник USD-фактов с задержкой 15 минут и лимитом 100 запросов/сутки. Polymarket — первый источник цен конкретных сценариев. MQL5 web — экспериментальное подключение для forecast snapshots, выключенное по умолчанию; официальный MT5 API требует отдельного bridge.

Быстрый бесплатный consensus не обещается. CME FedWatch EOD и intraday рассматриваются как отдельные платные расширения. Подробности и первоисточники — в [матрице источников](docs/sources.md).

## Проверить текущие артефакты

Установить [uv](https://docs.astral.sh/uv/getting-started/installation/), затем из корня репозитория:

```bash
uv sync --locked
uv run python scripts/validate_contracts.py
```

Проверка не использует API-ключи и не отправляет сообщения. Она валидирует схемы, положительные примеры, отрицательные случаи и конфигурацию. Она не проверяет доступность провайдеров, runtime или Telegram-доставку.

`Dockerfile` и `compose.yaml` появятся с первым запускаемым replay worker, затем — live adapter и Telegram sender. `.env.example` содержит только имена переменных. Реальные токены хранятся локально; Apache 2.0 относится к коду, условия поставщиков данных действуют отдельно.
