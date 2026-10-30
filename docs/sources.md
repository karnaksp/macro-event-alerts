# Источники и возможности

Проверено по документации поставщиков 30 октября 2026. Реализованы XOOMAR, MQL5 web и Polymarket; остальные адаптеры запланированы; наличие API не означает подтверждённое покрытие каждой серии или SLA. Лицензия кода Apache 2.0 не даёт прав на перепубликацию данных поставщиков.

| Источник | Доступ | Данные | Место в плане / ограничения |
| --- | --- | --- | --- |
| [XOOMAR](https://xoomar.com/markets/api/calendar) | Без ключа, опубликовано 10 запросов/мин на IP | actual, prior, расписание | Live adapter включён; Consensus отсутствует; обновления могут запаздывать примерно на час и больше. Покрытие каждой серии проверяется отдельно. |
| [FXMacroData](https://fxmacrodata.com/documentation/quickstart) | Без ключа: USD, 90 дней, 100 запросов/сутки | Факт и предыдущий результат | Задержка 15 минут; бесплатные прогнозы отсутствуют. Бюджет делится между всеми сериями, повторами и страницами. |
| [MQL5 Calendar](https://www.mql5.com/en/docs/calendar) | Официальные функции внутри MT5 | actual, forecast, previous, revision | MT5 использует время торгового сервера и scale 10⁶. Это отдельный bridge, а не публичный Linux REST API. |
| [MQL5 web calendar](https://www.mql5.com/en/economic-calendar) | Общедоступный сайт | Поля календаря и прогнозов | Недокументированный HTTP endpoint `/economic-calendar/content`: эксперимент, явно включён в config.live.yaml; квоты/SLA не подтверждены. Web и MT5 timezone нельзя отождествлять. |
| [Polymarket](https://docs.polymarket.com/getting-started/api) | Public market data без credentials | Метаданные исходов, цены, стакан | Live scenario adapter: точный market/outcome ID, правила разрешения, midpoint/last trade, спред и ликвидность. Это рыночная цена отдельного контракта. |
| [Kalshi](https://docs.kalshi.com/getting_started/quick_start_market_data) | Несколько публичных REST endpoints без ключа | events, markets, orderbook | Следующий scenario adapter. Не предполагаем анонимный доступ ко всему API или WebSocket. |
| [BLS](https://www.bls.gov/bls/newsrels.htm), [BEA](https://www.bea.gov/news), [Federal Reserve](https://www.federalreserve.gov/feeds/feeds.htm) | Бесплатные публикации и API/RSS | Официальные факты, пересмотры, решения | Контроль результатов и позднейшие адаптеры. Не дают универсальный consensus; доступность и задержка API отличаются от времени пресс-релиза. |
| [FRED](https://fred.stlouisfed.org/docs/api/fred/) | Бесплатный API с ключом | История, vintages, series | История/пересмотры; не обещает самый ранний результат и не даёт consensus. Нужны преобразования series. |
| [CME FedWatch](https://www.cmegroup.com/market-data/market-data-api/fedwatch-api.html) | Платный API | Вероятности ставок по фьючерсам | Опциональное расширение: EOD и intraday — разные продукты. Бесплатную веб-страницу не считаем стабильным бесплатным API. |

## Календарь и обновления прогноза

В [MQL5 структурах](https://www.mql5.com/en/docs/constants/structures/mqlcalendar) есть `forecast_value` и `revised_prev_value`, но старое значение forecast не передаётся. [CalendarValueLast](https://www.mql5.com/en/docs/calendar/calendarvaluelast) возвращает изменения по `change_id`; изменение прогноза определяется сравнением сохранённых снимков. `revised_prev_value` — пересмотр прошлого факта, а не прошлый прогноз.

Forecast origin хранится как `consensus`, `model`, `official_projection` или `unknown`. Название поля `forecast` само по себе не подтверждает consensus. Разные поставщики сравниваются отдельно. Платный календарь с consensus можно подключить позже после проверки актуальной цены, лицензии, покрытия и обновлений; подписки автоматически не приобретаются.

## Вероятности сценариев

[Polymarket prices/order books](https://docs.polymarket.com/market-data/prices-order-books) различают last trade и midpoint. Last-trade endpoint может вернуть `0.5` с пустой стороной для рынка без сделок: такой placeholder не является измеренной вероятностью. Старый last trade, пустая книга, большой спред, неизвестная ликвидность или смена критериев разрешения подавляют алерт по правилам качества.

Для FedWatch [EOD API](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457320466/CME%2BFedWatch%2BEnd-of-Day%2BAPI) за $25/месяц обновляется в рабочие дни в 01:45 UTC. [Intraday API](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/1054343170/CME%2BFedWatch%2BIntraday%2BAPI) имеет частоту 60 секунд и отдельные OAuth/entitlements. В [тарифах июня 2026](https://www.cmegroup.com/market-data/files/analytics-data-fee-list-june-2026.pdf) non-display intraday указан как $280/приложение/месяц с возможными дополнительными connectivity fees. Классификация бота и полная стоимость не подтверждены. Поэтому $25 не закладывается как бюджет intraday.

## Начальное покрытие

Целевые серии: CPI/Core CPI, PCE/Core PCE (MoM и YoY — разные ID), NFP monthly change, unemployment, GDP QoQ annualized, решение FOMC. ECI включается после проверки фактических релизов. NFP total employment не подменяет monthly change; ставка midpoint не подменяет диапазон target rate. Сквозной MVP начинается с 2–3 подтверждённых серий и одного проверенного сценария, остальные добавляются по fixtures.

Все JSON в `examples/` синтетические; они не являются текущими результатами статистики или котировками.

Проверка live от 30 октября 2026: 6 XOOMAR, 13 MQL5, 1 Polymarket observation прошли canonical validation внутри контейнера. MQL5 listing не сообщает period: хранится `unknown` и стабильный vendor release ID. Полный отчёт — `docs/live-verification.md`.


## Расширение покрытия MQL5

Проверено 30 октября 2026 по страницам самого источника. Добавлены 9 серий; вместе с прежними это 22 MQL5-серии и отдельный XOOMAR midpoint ставки ФРС — всего 23 ряда.

| Ряд | Проверенные страницы MQL5 | Единицы |
| --- | --- | --- |
| PPI | [m/m](https://www.mql5.com/en/economic-calendar/united-states/producer-price-index-mm), [y/y](https://www.mql5.com/en/economic-calendar/united-states/producer-price-index-yy) | %; месяц/год отдельно |
| Core PPI без еды и энергии | [m/m](https://www.mql5.com/en/economic-calendar/united-states/producer-price-index-ex-food-energy-mm), [y/y](https://www.mql5.com/en/economic-calendar/united-states/producer-price-index-ex-food-energy-yy) | %; месяц/год отдельно |
| Розничные продажи | [m/m](https://www.mql5.com/en/economic-calendar/united-states/retail-sales-mm), [без автомобилей](https://www.mql5.com/en/economic-calendar/united-states/retail-sales-ex-autos-mm) | % за месяц; ex-autos не равен ex-autos-and-gas |
| Первичные заявки на пособие | [Initial Jobless Claims](https://www.mql5.com/en/economic-calendar/united-states/initial-jobless-claims) | Люди, еженедельно; K/M преобразуются в units |
| ISM Manufacturing PMI | [Промышленность](https://www.mql5.com/en/economic-calendar/united-states/ism-manufacturing-pmi) | Уровень индекса, ежемесячно |
| ISM Non-Manufacturing PMI | [Услуги](https://www.mql5.com/en/economic-calendar/united-states/ism-non-manufacturing-pmi) | Уровень индекса, ежемесячно |

Для всех новых рядов включены release/revision и forecast-change правила MQL5. Synthetic fixtures проверяют mapping и единицы; текущий календарь может содержать не все последние факты одновременно. Исторические наблюдения сохраняются с is_backfill=true без изменения live baseline и очереди, но доступны ручной сводке. Неизвестные периоды MQL5 не восстанавливаются по дате релиза.


## Китай, еврозона и Россия

Проверено 30 октября 2026. MQL5 country156/CNY относится к [Китаю](https://www.mql5.com/en/economic-calendar/china); country999/EUR использует путь european-union, но описания выбранных рядов относятся к еврозоне. [CalendarEventByCurrency](https://www.mql5.com/en/docs/calendar/calendareventbycurrency) подтверждает source country999. Канонический country EA обозначает агрегат еврозоны.

| Регион / canonical series | Первичная страница | Единицы / ограничение |
| --- | --- | --- |
| CN.CPI.HEADLINE.MOM | [cpi-mm](https://www.mql5.com/en/economic-calendar/china/cpi-mm) | % |
| CN.CPI.HEADLINE.YOY | [cpi-yy](https://www.mql5.com/en/economic-calendar/china/cpi-yy) | % |
| CN.PPI.HEADLINE.YOY | [ppi-yy](https://www.mql5.com/en/economic-calendar/china/ppi-yy) | % |
| CN.GDP.QOQ | [gdp-qq](https://www.mql5.com/en/economic-calendar/china/gdp-qq) | %; без annualization |
| CN.GDP.YOY | [gdp-yy](https://www.mql5.com/en/economic-calendar/china/gdp-yy) | %; без annualization |
| CN.NBS.MANUFACTURING.PMI | [manufacturing-pmi](https://www.mql5.com/en/economic-calendar/china/manufacturing-pmi) | уровень индекса; NBS, не Caixin |
| CN.NBS.NON_MANUFACTURING.PMI | [non-manufacturing-pmi](https://www.mql5.com/en/economic-calendar/china/non-manufacturing-pmi) | уровень индекса; NBS, не Caixin |
| CN.INDUSTRIAL.PRODUCTION.YOY | [industrial-production-yy](https://www.mql5.com/en/economic-calendar/china/industrial-production-yy) | % |
| CN.RETAIL.HEADLINE.YOY | [retail-sales-yy](https://www.mql5.com/en/economic-calendar/china/retail-sales-yy) | % |
| CN.UNEMPLOYMENT.RATE | [unemployment-rate](https://www.mql5.com/en/economic-calendar/china/unemployment-rate) | % |
| EA.HICP.HEADLINE.MOM | [consumer-price-index-mm](https://www.mql5.com/en/economic-calendar/european-union/consumer-price-index-mm) | % |
| EA.HICP.HEADLINE.YOY | [consumer-price-index-yy](https://www.mql5.com/en/economic-calendar/european-union/consumer-price-index-yy) | % |
| EA.HICP.CORE.MOM | [consumer-price-index-ex-food-energy-mm](https://www.mql5.com/en/economic-calendar/european-union/consumer-price-index-ex-food-energy-mm) | % |
| EA.HICP.CORE.YOY | [consumer-price-index-ex-food-energy-yy](https://www.mql5.com/en/economic-calendar/european-union/consumer-price-index-ex-food-energy-yy) | % |
| EA.GDP.QOQ | [gross-domestic-product-qq](https://www.mql5.com/en/economic-calendar/european-union/gross-domestic-product-qq) | %; без annualization |
| EA.GDP.YOY | [gross-domestic-product-yy](https://www.mql5.com/en/economic-calendar/european-union/gross-domestic-product-yy) | %; без annualization |
| EA.UNEMPLOYMENT.RATE | [unemployment-rate](https://www.mql5.com/en/economic-calendar/european-union/unemployment-rate) | % |
| EA.RETAIL.HEADLINE.MOM | [retail-sales-mm](https://www.mql5.com/en/economic-calendar/european-union/retail-sales-mm) | % |
| EA.INDUSTRIAL.PRODUCTION.MOM | [industrial-production-mm](https://www.mql5.com/en/economic-calendar/european-union/industrial-production-mm) | % |
| EA.ECB.DEPOSIT.RATE | [ecb-deposit-rate-decision](https://www.mql5.com/en/economic-calendar/european-union/ecb-deposit-rate-decision) | % |
| EA.ECB.MAIN_REFINANCING.RATE | [ecb-interest-rate-decision](https://www.mql5.com/en/economic-calendar/european-union/ecb-interest-rate-decision) | % |
| EA.SP_GLOBAL.MANUFACTURING.PMI | [markit-manufacturing-pmi](https://www.mql5.com/en/economic-calendar/european-union/markit-manufacturing-pmi) | уровень индекса; текущая страница N/D, стадию flash/final не угадываем |
| EA.SP_GLOBAL.SERVICES.PMI | [markit-services-pmi](https://www.mql5.com/en/economic-calendar/european-union/markit-services-pmi) | уровень индекса; текущая страница N/D, стадию flash/final не угадываем |
| EA.SP_GLOBAL.COMPOSITE.PMI | [markit-composite-pmi](https://www.mql5.com/en/economic-calendar/european-union/markit-composite-pmi) | уровень индекса; текущая страница N/D, стадию flash/final не угадываем |

[Россия не представлена в текущем списке MQL5](https://www.mql5.com/en/economic-calendar); ссылки russia/russian-federation возвращали404, RUB отсутствует в фильтре. Поэтому используются официальные источники:

- [Решение Банка России](https://www.cbr.ru/press/keypr/): RU.CBR.KEY_RATE, точная дата решения и явно указанное следующее заседание/время13:30МСК. Timestamp обновления с явным timezone хранится как source_updated_at; timezone-naive footer остаётся неизвестным, дата обновления не считается доказанным временем публикации.
- [Инфляция и ключевая ставка](https://www.cbr.ru/hd_base/infl/): RU.CPI.HEADLINE.YOY, месячный отчётный период MM.YYYY, колонка инфляции y/y. Ставка на конец месяца из соседней колонки не используется как текущее решение.

CBR adapter не предоставляет forecast. В live выдаётся только последний месяц CPI, текущее решение и подтверждённое будущее заседание. Новый месяц CPI после существующего live baseline создаёт release; первая загрузка и backfill остаются тихими. GDP, рынок труда и производство России этим адаптером не покрыты; неподтверждённые mappings не добавлены.
