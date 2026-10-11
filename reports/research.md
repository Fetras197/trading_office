# Ресерч: мультиагентная торговая фирма (данные на 2026-10-12, GitHub API)

## 1. Сравнение фреймворков

| Проект | Звёзды | Последний push | Лицензия | Стек | LLM |
|---|---|---|---|---|---|
| TauricResearch/TradingAgents | 110.6k | 2026-10-03 (v0.6.0) | Apache-2.0 | LangGraph | Gemini, Claude, GPT, Grok, DeepSeek, Ollama, Bedrock и др. |
| ZhuLinsen/daily_stock_analysis | 66.2k | 2026-10-11 | MIT | FastAPI + cron/GH Actions | Claude, Gemini, OpenAI-совм., Ollama |
| virattt/ai-hedge-fund | 63.9k | 2026-10-02 | MIT | LangGraph | Anthropic, Google, OpenAI, DeepSeek, xAI |
| AI4Finance-Foundation/FinRobot | 8.2k | 2026-10-11 | Apache-2.0 | AutoGen → OpenAI Agents SDK → PydanticAI (V2) | в основном OpenAI/Azure, агностик |

Все четыре живые. (`AI4Finance/FinRobot` переехал в организацию `AI4Finance-Foundation`.)

### TradingAgents
- Архитектура ≈ ровно наш список ролей: 4 аналитика (fundamentals, sentiment, news, technical) → bull/bear дебаты → trader → risk team → portfolio manager approve/reject.
- Данные: SEC EDGAR, Yahoo Finance, FRED, StockTwits/Reddit. Крипта через Yahoo (`BTC-USD`) — без деривативов/ончейна.
- Gemini и Claude поддерживаются нативно. Есть checkpoint-резюм, память решений.
- Взять: структуру дебатов и промпты ролей, идею «decision settlement» (сверка решения с исходом — основа точности по ролям).
- Не брать как зависимость: тяжёлый LangGraph, нет исполнения на биржах, нет жёстких лимитов риска в коде.

### ai-hedge-fund
- Агенты-«инвесторы» (Buffett, Burry и т.п.) + risk manager + portfolio manager. Явно «не для реальной торговли».
- Данные: платный Financial Datasets API. Крипты нет.
- Есть бэктестер и paper-ledger. Взять: формат бэктеста (equity-curve vs бенчмарк), идею risk manager, который считает лимиты позиции детерминированно, а не LLM.

### FinRobot
- Слои: агенты → LLM-алгоритмы → DataOps → модели. 56 playbook-ов аналитиков, 32 детерминированных compute-оператора на чистом Python. Дебаты bull/bear/judge.
- Принцип «models reason, software computes, agents orchestrate, systems verify» — совпадает с нашим дизайном.
- Взять: детерминированные индикаторы/метрики отдельно от LLM; failover провайдеров данных (FMP, Finnhub, yfinance, EDGAR). Сам фреймворк — OpenAI-центричный, смена API между версиями (V0/V1/V2) → не зависеть.

### daily_stock_analysis
- Упор на ежедневный отчёт + «decision dashboard» + пуш (Telegram есть) + cron. Поддержка US-акций, но фундаментал полноценно только для A-shares. Бэктест вынесен в отдельный проект (AlphaEvo).
- Взять: паттерн расписания + пуш, мульти-провайдер LLM-абстракцию (litellm-подобная), fallback источников данных.

### Вывод
Готовый фреймворк целиком не подходит: ни один не даёт (а) крипту с деривативами, (б) исполнение на testnet/paper с лимитами в коде, (в) журнал точности по ролям. Берём дизайн TradingAgents, детерминизм FinRobot, бэктест-формат ai-hedge-fund, расписание/пуш daily_stock_analysis. Код пишем свой, маленький.

## 2. MCP-серверы

| Область | Сервер | Звёзды | Обновлён | Вердикт |
|---|---|---|---|---|
| Акции/крипта, paper | alpacahq/alpaca-mcp-server (официальный) | 1016 | 2026-10-11 | ЖИВОЙ. MIT, `uvx alpaca-mcp-server`, `ALPACA_PAPER_TRADE=true` по умолчанию, 80+ tools, `get_news` |
| CCXT | doggybee/mcp-server-ccxt | 145 | 2026-09-16 | жив, TS, неофициальный |
| CCXT | lazy-dinosaur/ccxt-mcp | 95 | 2026-10-06 | жив, TS |
| CCXT | Nayshins/mcp-server-ccxt | 62 | 2026-10-09 | жив, Python, только market data (безопасно) |
| Binance | AnalyticAce/binance-mcp-server | 50 | 2026-10-02 | жив, Python/FastMCP, неофициальный |
| Binance | nirholas/Binance-MCP, ethancod1ng/binance-mcp-server | 37 / 34 | 2026-09/10 | живы, TS, неофициальные |
| Hyperliquid | mektigboy/server-hyperliquid | 44 | 2026-08-01 | жив-ish, 2.5 мес без коммитов |
| Hyperliquid | caiovicentino/hyperliquid-mcp-server, kukapay/hyperliquid-info-mcp | 36 / 30 | 2026-09/10 | живы; info-mcp read-only |
| Новости/сентимент (крипта) | zkoranges/cryptominute-news-mcp, cryptobriefing-labs/gloria-mcp, deficlow/zipp-mcp | 0-2 | 2026-02…08 | живые, но мизерные (1-2★), хостинг/ключи сторонние → ненадёжно |
| Новости/сентимент (акции+крипта) | Alpaca `get_news` | — | — | лучший вариант, официальный |

Официального MCP от Binance и CCXT в GitHub-org не найдено (поиск `org:binance mcp`, `org:ccxt mcp` — 0). Все Binance/CCXT серверы — комьюнити, мало звёзд, нет аудита.

**Критичный вывод по безопасности:** не подключаем торговые MCP агентам. Неофициальный код с API-ключами + LLM, способный вызвать `create_order`, обходит лимиты риска. Решение:
- Данные собирает Python (`ccxt` напрямую, `yfinance`/Alpaca data API, Binance public futures API для funding/OI, Alpaca news) и кладёт агентам в промпт как JSON → воспроизводимо и бэктестится на тех же функциях.
- Исполнение — только Python-класс `Executor` после `RiskGate`, в паре с `SimBroker` (или `AlpacaPaperBroker`). LLM-агент `executor` в `.claude/agents/` лишь формирует/проверяет ордер-тикет (read-only), реальные ордера шлёт код.
- Alpaca MCP — опционально, read-only (account/positions/news), для интерактивной работы человека в Claude Code; в проект не вшиваем.

## 3. Источники данных (проверяемые)
- **Решение владельца: Binance Testnet не используется (гео-блок).** Исполнение крипты — собственный paper-брокер (симулятор): цены с публичного `data-api.binance.vision` (без ключей), баланс/позиции ведём сами, комиссия 0.1%, проскальзывание 0.05%, все сделки в SQLite. Тот же брокер используется в бэктесте → live-paper и бэктест идентичны по исполнению.
- Binance Futures публично: funding rate, open interest, long/short ratio → роль onchain_derivs. Настоящий ончейн (поток на биржи, whales) бесплатно и надёжно не получить → честно: «onchain» = деривативные метрики + публичные ончейн-метрики, если доступны (blockchain.info/mempool), иначе роль работает на деривативах.
- Акции: Alpaca Paper (ордера + data API), только если в `.env` есть ключи; без ключей акции идут в тот же симулятор с ценами `yfinance`. Фундаментал — yfinance `info` + SEC EDGAR.
- Новости: Alpaca news (акции+крипта), CryptoPanic/RSS как дополнение.

## 4. Ограничения, которые надо знать заранее
1. **Бэктест LLM-ролей** страдает look-ahead: модель знает историю до cutoff. 6 мес. (апр–окт 2026) частично после cutoff Sonnet 5.5 (июнь 2026), но не полностью. В отчёте это помечаем.
2. **Исторических новостей/фундаментала** для честного бэктеста нет бесплатно → в бэктесте участвуют роли с историческими данными (tech, derivs, risk, trader, PM); news/fundamental получают пометку «нет данных» и исключаются из точности.
3. **Стоимость:** 11 ролей × 6 активов × цикл раз в N часов. Бэктест на LLM по всем барам нереалистичен → бэктест по умолчанию детерминированный (rule-based версии ролей), LLM-режим — на сэмпле (1 решение/день/актив) с кэшем.
4. Запуск из proot для оркестратора-демона нельзя (умирает с сессией) → только Termux, команды выдаём в конце.
