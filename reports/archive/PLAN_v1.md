# PLAN: trading_office — мультиагентная торговая фирма (paper only)

## Цель
Цикл раз в N часов по BTC, ETH, SOL (крипта) + AAPL, MSFT, NVDA (акции США): 11 ролей-субагентов выдают мнения, код применяет жёсткие риск-лимиты и исполняет ТОЛЬКО на paper (собственный симулятор; Alpaca Paper для акций, если есть ключи). Журнал SQLite, точность по ролям, Telegram-отчёт с кнопками, бэктест 6 мес.

## Принципы
1. **LLM советует, код решает.** Лимиты риска, размер позиции, стоп и исполнение — детерминированный Python. LLM не имеет доступа к ордерам и ключам.
2. **Данные собирает Python**, агентам уходит JSON в промпте (воспроизводимо, пригодно для бэктеста). Торговые MCP не подключаются.
3. **Одинаковое исполнение в live-paper и бэктесте** (один `SimBroker`).
4. Секреты только в `.env`. Live-режима нет вообще: в коде нет live-эндпоинтов.

## Архитектура
```
trading_office/
  .claude/agents/*.md        11 ролей (frontmatter + системный промпт, JSON-ответ)
  office/
    config.py                активы, лимиты, интервал, пути (.env)
    data.py                  свечи: data-api.binance.vision (крипта), yfinance (акции); funding (fapi, best-effort); новости (Alpaca/RSS, best-effort)
    indicators.py            EMA/RSI/MACD/ATR/BB на pandas (детерминированно)
    llm.py                   backend: claude -p (по умолч.), gemini, fake (тесты); парсинг/валидация JSON, ретраи
    roles.py                 пайплайн ролей, схемы ответов, rule-based версии ролей для бэктеста
    risk.py                  RiskGate (лимиты)
    broker.py                Broker ABC; SimBroker (баланс, комиссия 0.1%, проскальзывание 0.05%, SQLite); AlpacaPaperBroker (опц.)
    journal.py               SQLite: runs, opinions, decisions, orders, fills, equity, outcomes
    accuracy.py              расчёт точности по ролям
    approval.py              Telegram: inline-кнопки Одобрить/Отклонить (Bot API через requests, getUpdates)
    notify.py                обёртка tgnotify
    orchestrator.py          цикл, расписание, --once, --dry-run, --auto
    backtest.py              6 мес., отчёт reports/backtest.md
  tests/
  reports/research.md, backtest.md
```

## Поток одного решения (актив A)
1. `data.bundle(A)` → свечи 1h/4h/1d, индикаторы, funding/OI, новости, фундаментал.
2. Параллельно: tech_analyst, fundamental_analyst, news_sentiment, onchain_derivs → `{stance: buy|sell|hold, confidence 0..1, rationale}`.
3. bull_researcher и bear_researcher получают отчёты аналитиков → аргументы + `case_strength`.
4. trader → `{action: buy|sell|hold, entry, stop, take_profit, risk_pct}`.
5. risk_manager (LLM-мнение) + **RiskGate (код, главный)**: стоп обязателен; убыток до стопа ≤2% эквити; нотионал ≤25% эквити; дневной стоп −5%; ≤5 сделок/день; режим long-only (sell = закрытие позиции). RiskGate только режет/отклоняет.
6. portfolio_manager → approve/reject/уменьшить (увеличить не может).
7. Если `APPROVAL=telegram`: сообщение с кнопками; таймаут → отклонить. `--auto` — без кнопок.
8. executor (LLM) делает pre-flight проверку тикета (read-only); `Broker` исполняет.
9. journal (LLM) пишет пост-анализ; код фиксирует всё в SQLite.
10. Settlement: через горизонт H (по умолч. 24ч) считается forward return; мнение роли «верно», если buy→ret>+θ, sell→ret<−θ, hold→|ret|≤θ. Точность по ролям = доля верных.

## Риск-лимиты (в коде, не в промптах)
- Риск на сделку: `qty*|entry-stop| ≤ 2%` эквити (интерпретация «≤2% на сделку»; плюс нотионал ≤25%).
- Дневной стоп: equity_now/equity_day_start−1 ≤ −5% → блок новых входов до следующих UTC-суток.
- ≤5 исполненных ордеров на вход/день (UTC).
- Акции: только в часы рынка; крипта 24/7.
- Kill-switch: файл `STOP` в корне → цикл не торгует.

## Бэктест (фаза 3)
- 6 мес. 1h-свечи (крипта: data-api.binance.vision; акции: yfinance 1h), шаг решения 4ч, тот же RiskGate/SimBroker/журнал.
- **Основной режим — rule-based роли** (tech: EMA/RSI/MACD, derivs: funding; fundamental/news: «нет исторических данных» → исключены из точности). LLM-режим: `--llm --sample daily` с кэшем, без утечки будущего в промпт; look-ahead bias модели в отчёте помечается.
- Метрики: винрейт, PnL, макс. просадка, Sharpe(простой), количество сделок, buy&hold бенчмарк, точность по ролям.

## Этапы
1. Скелет, config, indicators, data (+тесты на фикстурах).
2. broker/SimBroker + risk + journal (+тесты на лимиты — критично).
3. Агенты .claude/agents + llm.py + roles.py + orchestrator.
4. approval/notify.
5. backtest + запуск + reports/backtest.md.
6. ruff, pytest, relay-review, gitsave, отчёт.

## Риски
- Сеть/гео: Binance Testnet заблокирован → не используем. Публичный `data-api.binance.vision` — проверить доступность.
- Стоимость LLM: ~10 вызовов × 6 активов за цикл → дефолт интервал 4ч, модель на роль настраивается (haiku для аналитиков).
- Некорректный JSON от LLM → строгая валидация, при ошибке роль = hold/abstain (не блокирует цикл).
- Исторические новости/фундаментал недоступны → ограничение бэктеста.
- Оркестратор — только Termux (не proot).

## Критерии приёмки
- [ ] 11 файлов в `.claude/agents/`
- [ ] Тесты RiskGate: нарушение любого лимита блокирует/режет ордер
- [ ] SimBroker: комиссия 0.1%, проскальзывание 0.05%, баланс сходится с журналом
- [ ] `python -m office.orchestrator --once --dry-run` проходит цикл по 6 активам (fake LLM)
- [ ] Журнал содержит мнение каждой роли и итог; `accuracy` выдаёт таблицу по ролям
- [ ] `reports/backtest.md` с метриками
- [ ] pytest зелёный, ruff чистый, gemini-ревью, gitsave

## Итог gemini-plan (учтено / отклонено)
Принято и меняет план (приоритет над текстом выше):
- **#1/#3 look-ahead и нет истории новостей:** LLM-бэктест помечается «индикативный»; основной вывод бэктеста — по пайплайну риск/исполнение и rule-based базлайну.
- **#2 разрыв бэктест/live:** в каждом live-цикле дополнительно считается и журналируется «shadow» rule-based решение. Отчёт сравнивает LLM-фирму и базлайн на одних данных.
- **#4 усталость от approve:** кнопки только на сделки, прошедшие RiskGate (hold — без сообщений); один дайджест на цикл.
- **#5 RiskGate vs стоп:** RiskGate НЕ двигает стоп. Режет только количество (`qty = 2%·equity / |entry−stop|`, ≤25% нотионала); сделка отклоняется, если стоп ближе 0.5·ATR или дальше 4·ATR. R:R сохраняется.
- **#6 лимиты API:** семафор параллелизма (3), таймаут на вызов, ретраи с backoff, short-circuit: если все 4 аналитика hold с confidence<0.5 — дебаты/трейдер пропускаются (~70% экономии токенов в спокойном рынке).
- **#7 акции/выходные:** актив пропускается, если рынок закрыт (проверка по часам NYSE через `zoneinfo` + устаревание последней дневной свечи для праздников); цикл не ждёт.
- **#8 проскальзывание:** по заданию владельца 0.05%; в бэктесте добавляется стресс-прогон 0.20%.
- **#9 settlement:** для исполненных сделок исход = реальный результат (R-мультипл по стопу/тейку/тайм-ауту). Для мнений без сделки — triple-barrier (±1·ATR или тайм-аут H=24ч), а не «цена через 24ч».
Отклонено:
- **#10 сократить роли:** 11 ролей — требование владельца; нагрузку снижает short-circuit (#6). Дублирование bull/bear проверяется метрикой точности по ролям — слабые роли видны в отчёте.

## Уточнение по данным (проверено 2026-10-12 из proot)
- data-api.binance.vision klines — работает (в т.ч. история).
- fapi.binance.com и api.bybit.com — гео-блок → деривативы (funding, OI, premium) с **Hyperliquid info API** (работает, funding hourly с историей) — это и источник для бэктеста.
- Акции: Yahoo chart API напрямую через requests (без yfinance). Новости: Google News RSS (keyless, best-effort). Фундаментал акций: yfinance, если установлен, иначе роль воздерживается.
- ccxt не нужен: публичные REST-вызовы через requests.
