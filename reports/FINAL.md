# FINAL — «Торговый зал» trading_office v2 (2026-10-12)

Статус: **собрано, протестировано, один полный живой цикл завершился paper-ордером Alpaca. Демон в Termux не запускался — только вы** (`./office.sh start`). Старый v1 — `reports/archive/`.

## Карта офиса
```
скринер (без LLM, 15 мин)  ──►  top-5 кандидатов (board.candidates)
 TA-Lib: ROC20/RSI/объём vs SMA20/пробой 20д/сигнал lab        │
 все крипто-пары Alpaca (36) + S&P500∪NDX (518)                ▼
supervisor (1 ч): BTC/ETH/SOL · SPY/NVDA/AAPL/MSFT в часы биржи · top-5 · открытые позиции   (≤10 активов, 2 параллельно)
   │
   ▼  по каждому активу — цепочка, каждый отчёт на шине board.db {id, role, asset, ts, вывод, уверенность 0-1, ссылки_на, данные}
 Аналитики (Gemini):  market (TA-Lib + TradingView) · news (заголовки, FinBERT-стиль) · fundamentals (equity-research) · quant (wshobson)
                      · buffett / munger / druckenmiller (ai-hedge-fund, акции) · lab (approved-стратегии, голос без LLM)
   ▼  Bull ⇄ Bear ×2 раунда (промпты TradingAgents; Claude)  — обязаны цитировать аналитиков и оппонента
   ▼  Trader (Claude) ► Risk (Claude + wshobson risk-manager + Riskfolio-Lib CVaR) ► PM (Claude + PyPortfolioOpt)
   ▼  RiskGate (КОД: лимиты) ► Executor-проверка (Claude haiku) ► Alpaca PAPER (market + защитный stop / bracket для акций)
   ▼  Telegram: сделка с цепочкой «кто что сказал, на кого сослался (✓ согласен / ✗ оспорил)»; сводка раз в 6 ч
Лаборатория (фон, nice): vectorbt walk-forward → board.strategies → approved становятся аналитиком 'lab'
```
Правило ссылок — `office/board.py`: после аналитиков нужен ≥1 существующий отчёт того же цикла/актива + явный agree/dispute по каждой ссылке + обязательные роли (bear→bull, trader→bull+bear, risk→trader, PM→trader+risk, executor→PM); иначе `RejectedReport` (в `rejected`), одна попытка с текстом причины, затем пропуск актива. В живых циклах правило сработало 3 раза (bear/trader не процитировали оппонента) — отчёты отклонены, повтор прошёл.

## Жёсткие лимиты (код, тесты `test_risk.py`, `test_alpaca.py`, `test_hall.py`, `test_supervisor.py`)
paper-only (`ALPACA_PAPER!=true` или не-paper хост → отказ) · cash 1x (размер ≤ cash) · только long, sell лишь закрывает существующий лонг, опционов нет · риск до стопа с комиссией и проскальзыванием ≤2% эквити, стоп обязателен · нотионал ≤25% · дневной стоп −5% (блок входов) · ≤5 входов/сутки (UTC) · ≤3 позиции · невалидный ответ/лимиты LLM/нет ссылок → пропуск актива · файл `STOP` (`halt`) блокирует новые решения. Лимиты исчерпаны → цикл актива прерывается до трат на LLM. Исполнение под общим локом со свежим состоянием счёта.

## Команды (Termux)
```
cd ~/projects/trading_office
./office.sh start      # supervisor + лаборатория (wake-lock, proot ubuntu, --no-kill-on-exit)
./office.sh status     # процессы, HALT, equity, позиции, кандидаты, число approved
./office.sh halt       # новые решения запрещены (позиции под защитными стопами);  ./office.sh resume — снять
./office.sh stop       # остановить (watchdog не поднимет)
./office.sh once BTC/USD [--dry-run]   # один полный цикл
./office.sh log 80 [supervisor|lab]
crontab -e  →  */10 * * * * /data/data/com.termux/files/home/projects/trading_office/office.sh watchdog   (строка в reports/crontab.txt; нужен запущенный `crond`)
```
Для 24/7 в Android: отключить оптимизацию батареи для Termux, держать `termux-wake-lock` (start берёт сам), `crond` запущен.
Прочее: `python -m office screen|summary|accuracy|status` (внутри proot, venv `.venv`), тесты `.venv/bin/python -m pytest`.

## Приёмка
- **pytest: 68 passed (на 2026-10-12 вечер)**, ruff F/E9 чист.
- **Живой полный цикл с ордером — ONDO/USD (естественный вход, не подстановка).** Скринер отдал ONDO в top-5; все роли ответили живьём: 4 аналитика (fundamentals — честный abstain, HTTPError CoinGecko для альта), bull/bear ×2, trader buy 0.55, risk_manager *reduce*, PM approve, RiskGate, executor ok. Ордер Alpaca paper: **market buy `43748db5-eb4f-4e3a-8768-a9cf7332c589`**, filled 29049.61 @ 0.51697; защитный stop_limit `c9bdd130-111b-4bb1-8d21-8496b54117e6` (stop 0.5033 / limit 0.4983), take 0.5417 исполняет бот. Цепочка отчётов #273–#284 в `board.db`, текст — `reports/acceptance_chain_ONDO.md`. Позиция осталась открытой, пока supervisor не запущен — она под биржевым стопом; тейк и учёт закрытия подхватит `watch` после `./office.sh start`.
- **BTC: ответы всех ролей живые (≥6 циклов, цепочка до trader включительно), но trader каждый раз сказал hold** (отрицательный 20д-моментум quant, перекупленность) — ордера на BTC нет. Это не подгонялось (DECISIONS #29).
- Сквозная проверка акций: `once NVDA --dry-run` — все 7 аналитиков, включая Buffett/Munger/Druckenmiller, и дебаты ответили живьём (ордер не отправлялся: биржа закрыта).
- Скринер: 36 крипто-пар + 518 акций, скан ≈31 с. Лаборатория, 1-й прогон (BTC/ETH/SOL/SPY/NVDA/AAPL/MSFT × 4 семейства): **0 из 28 approved** — OOS не лучше buy&hold; AAPL rsi_revert близко (Sharpe 1.60 vs 1.54, просадка лучше, но 20 сделок < 30). Лаборатория пока не голосует в зале.
- Gemini-ревью: `relay-review` не запускал (он правит код параллельно с идущими живыми циклами); сделал прямое ревью через Gemini REST (`gemini-3.1-flash-lite`, 8 файлов, 12 пунктов; `reports/gemini_review_raw.md`). Принято и исправлено: (5) вход мог исполниться в момент отмены → теперь пересчитывается статус и поднимается явная тревога с ручной проверкой позиции; (9) при исчерпанных лимитах цикл актива прерывается до вызовов LLM. Отклонено как неверное прочтение кода: (1) look-ahead в walk-forward — параметры выбираются только на train-срезе, сигналы каузальны (тест `test_signals_are_causal`); (2) acct уже читается внутри лока; (4) аналитики — первый слой по ТЗ; (6) Journal и Board — разные файлы БД; (3,7,8,10,11,12) — не воспроизводятся/вне ТЗ.

## Известные ограничения и что делать вам
- **Ключи были присланы в чат → перевыпустить** (Alpaca paper, Gemini); новые — в `.env` (алиасы `ALPACA_API_KEY/ALPACA_SECRET_KEY`) и в `claude mcp` (alpaca).
- Gemini free-tier: при 503/429 аналитик → abstain (<2 живых → актив пропущен). Для 24/7 лучше платный ключ.
- Claude `-p`: ≈4.5 мин на актив; 10 активов × 2 параллельно ≈ 25 мин из 60. Если лимиты Claude кончатся — циклы пропускаются, процесс живёт.
- Альткойны без CoinGecko-id → fundamentals abstain (не критично, голос просто не учитывается).
- Trader осторожен: за первые ~30 циклов 1 вход. Это поведение, а не баг; оценивать только по накопленной точности ролей (`python -m office accuracy`, сводка 6 ч) и PnL vs SPY/BTC.
- FinBERT/torch не ставились (OOM) — сентимент по заголовкам через Gemini.
- MCP (alpaca, yfinance, coingecko, tradingview, плагины) — для ручной работы в Claude Code; торговый путь их не использует (DECISIONS #22). При обновлении плагина financial-analysis платные коннекторы включатся снова — отключить.
- Старый v1-бэктест (убыточный базлайн) и код — `reports/archive/`.


## Обновление 2026-10-12 (после запуска владельцем)
- **office.sh исправлен** (DECISIONS #31): бинд `~/projects → /root/projects`, `restart`, cron-строка без изменений (`*/10 * * * * .../office.sh watchdog`). Применить: `./office.sh restart`; через 2 минуты `./office.sh status` должен показать «supervisor: работает». Проверить в Termux самому: `tail -n 20 logs/supervisor.log`.
- **Telegram по-русски** (DECISIONS #32): покупка / продажа (стоп-лосс, тейк-профит, решение трейдера) / сводка 6 ч / служебные.
- **Фундаментальный аналитик** починен (DECISIONS #33).
- **Лаборатория** расширена: 11 семейств × 3 таймфрейма × стоп-лосс, крипта и акции раздельно (DECISIONS #34).
- pytest: 68 passed.
