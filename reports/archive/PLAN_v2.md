# PLAN v2: «Торговый зал» (paper only) — на готовых компонентах

Старый план/финал: reports/archive/. Старый код: переиспользуем `journal`, `risk`, `broker`/`alpaca` (executor), `telegram`, `llm`; остальное заменяется.

## Цель
Мульти-агентный зал: шина `board.db`, аналитики → bull/bear (2 раунда) → trader → risk → PM → executor (Alpaca paper). Скринер без LLM каждые 15 мин по всей вселенной, зал — только по top-5 + открытым позициям. Лаборатория (vectorbt walk-forward) даёт approved-стратегии как ещё одного аналитика. Работа 24/7 через `office.sh` из Termux.

## Компоненты (не писать своё)
| Роль | Компонент | Фоллбек |
|---|---|---|
| bull/bear, дебаты | TradingAgents `create_bull_researcher/create_bear_researcher` (prompts, 2 раунда), LLM = duck-типизированный адаптер `claude -p` | собственные промпты |
| Trader | TradingAgents trader/research_manager prompts | свой JSON-промпт |
| Инвесторы | ai-hedge-fund Buffett/Munger/Druckenmiller (prompts+скоринг на данных) | промпты-персонажи в Gemini |
| Market/TA | TA-Lib + TradingView MCP (tradingview-mcp-server) | pandas-ta-подобный `indicators.py` (есть) |
| Sentiment | FinBERT (ProsusAI/finbert) + yfinance news | Gemini-sentiment |
| Fundamentals | financial-services plugin (equity-research) промпт/скилл + yfinance | Gemini |
| Quant | wshobson quant-analyst (промпт из плагина) | Gemini |
| Risk | wshobson risk-manager + Riskfolio-Lib CVaR | PyPortfolioOpt CVaR |
| PM | PyPortfolioOpt (распределение) | равные веса |
| Лаборатория | vectorbt | собственный векторный бэктест на pandas |
| Исполнение | alpaca-py (`broker.py`, paper-only guard) | REST (есть) |

## Архитектура (office/)
- `board.py` — SQLite `board.db`: `reports(id, cycle_id, role, asset, ts, verdict, confidence, refs JSON, data JSON)`. `publish()` валидирует: после аналитиков refs обязателен (≥1 id отчёта предыдущих ролей из этого cycle_id/asset, существующий в БД) и в тексте должна быть явная позиция `stance_on_refs` (agree/dispute по каждой ссылке). Иначе `RejectedReport` → пропуск актива.
- `llm.py` — `ClaudeCLI` (`claude -p`, без хуков/MCP), `GeminiREST` (цепочка моделей), `invoke(prompt).content`; исчерпание лимитов → `LLMUnavailable` → роль `abstain`, цикл не падает (если нет ни одного валидного trader-ответа → пропуск).
- `analysts/` — market (TA-Lib+TradingView), news (FinBERT), fundamentals, quant, investors, lab (approved strategies). Gemini.
- `hall.py` — цепочка цикла по активу; 2 раунда bull/bear; trader → risk → PM → RiskGate (код) → executor.
- `risk.py` (есть) + `limits.py` — жёсткие лимиты в коде: paper-only, cash 1x, no short/options, ≤2% риска на сделку со стопом, дневной стоп −5%, ≤5 входов/сутки, ≤3 позиции, невалидный ответ → пропуск.
- `screener.py` — без LLM, каждые 15 мин: все крипто-пары Alpaca (USD) + S&P500 + NASDAQ-100 (списки с Wikipedia, кэш), метрики TA-Lib (моментум ROC/RSI, объём vs SMA20, пробой N-дневного high/low, сигналы approved-стратегий) → score → top-5 в `candidates`.
- `lab/` — vectorbt: перебор (SMA-cross, RSI mean-rev, Donchian breakout, MACD) × параметры; walk-forward train 4 мес / test 2 мес; approved ⇔ OOS Sharpe > buy&hold, OOS maxDD лучше buy&hold, ≥30 сделок OOS. Результаты в `strategies` таблицу board.db; approved → аналитик `lab` голосует по текущему сигналу.
- `supervisor.py` — циклы: крипта BTC/ETH/SOL каждый час; акции SPY/NVDA/AAPL/MSFT в часы NYSE (Alpaca clock); скринер 15 мин; лаборатория в фоне (отдельный процесс, nice); + top-5 скринера и открытые позиции. STOP-файл / `halt`.
- `report.py` — Telegram через `tgnotify`: на каждую сделку цепочка «кто что сказал и на кого сослался»; сводка раз в 6 ч (PnL vs SPY/BTC, позиции, точность ролей, находки лаборатории).
- `office.sh start|stop|status|halt|resume` — Termux → `proot-distro login ubuntu -- ...`; строка crontab watchdog.

## Этапы
1. Установка компонентов + проверка запуском → `reports/components.md`, фоллбеки в DECISIONS.md.
2. board.db + цепочка + правило ссылок.
3. Лимиты (перенос и расширение + тесты).
4. Скринер + лаборатория.
5. supervisor, office.sh, crontab.
6. Telegram.
7. Приёмка: pytest, живой цикл BTC (+ID ордера Alpaca), Gemini-ревью, gitsave, FINAL.md.

## Риски
- Android/proot: torch+transformers (FinBERT) и numba (vectorbt) на aarch64 могут не встать / не хватить RAM (1.7 ГБ свободно) → фоллбеки таблицы.
- Плагины/MCP через `claude plugin` и `uvx` в proot могут не работать (ранее `uvx alpaca-mcp-server` не запускался) → фиксируем причину, берём фоллбек (alpaca-py).
- Gemini free-tier лимиты; `claude -p` лимиты → abstain/пропуск.
- Ключи были присланы в чате → только `.env` (в .gitignore), рекомендовать перевыпуск.
- Живой цикл и демон нельзя запускать из сессии Claude Code (демон — только Termux); для приёмки — один разовый цикл `python -m office once BTC/USD`.
- Полный цикл на top-5 × (≈15 LLM-вызовов) укладывается в час только при ограничении параллелизма; ставим лимит на цикл (макс. активов/час).

## Приёмка
pytest зелёный; ruff чист; живой BTC-цикл с ответами всех ролей (или честно помеченным abstain у исчерпавших лимит) и paper-ордер с id; board.db содержит цепочку со ссылками; отклонение отчёта без ссылок покрыто тестом; Gemini-ревью; gitsave; FINAL.md.
