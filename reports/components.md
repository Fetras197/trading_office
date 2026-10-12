# Компоненты (2026-10-12 08:57)

| Компонент | Версия | Статус | Проверка |
|---|---|---|---|
| TradingAgents (vendor, ядро) | 0.6.0 @ 1394a3f 2026-10-03 | OK | bull_node |
| ai-hedge-fund (Buffett/Munger/Druckenmiller) | 78b779c 2026-10-02 | OK | You are Warren Buffett, evalua |
| Vibe-Trading (справочно) | d3c7681 2026-10-12 | OK | True |
| TA-Lib | 0.8.1 | OK | 53.1711085139517 |
| alpaca-py + Alpaca paper | 0.44.0 | OK | AccountStatus.ACTIVE equity=100000 |
| riskfolio-lib (CVaR) | 7.4.0 | OK | [[0.34, 0.31, 0.35]] |
| PyPortfolioOpt | 1.6.0 | OK | OrderedDict({'a': 0.256878700753154, 'b': 0.3870049549211864, 'c': 0.3561163443256596}) |
| vectorbt | 1.1.2 | OK | 0.096 |
| yfinance | 1.7.0 | OK | 5 |
| claude -p (Claude) | cli | OK | ok |
| Gemini REST | gemini-3.1-flash-lite | OK | ok |
| Claude plugins |  | OK | quantitative-trading, equity-research, financial-analysis |
| MCP (claude mcp list) |  | OK | plugin ✘ | github ✔ | futbin ✔ | context7 ✔ | alpaca ✔ | yfinance ✔ | coingecko ✔ | tradin |
| transformers/FinBERT | - | SKIP | решение владельца: torch не ставится (OOM) → сентимент Gemini/Claude по заголовкам, DECISIONS #16 |

## Ручной зонд MCP (stdio initialize + tools/list)
| MCP | Результат |
|---|---|
| alpaca (`uvx alpaca-mcp-server`) | OK, 72 инструмента (paper через ALPACA_PAPER_TRADE=true) |
| yfinance (`uvx --with 'mcp<2' yfinance-mcp`) | OK, 18 инструментов. Без пина `mcp<2` падает (`mcp.server.fastmcp` удалён в mcp 2.x) |
| coingecko (`npx -y @coingecko/coingecko-mcp`) | OK, 2 инструмента (execute, search_docs) |
| tradingview (`.venv/bin/tradingview-mcp`, pip `tradingview-mcp-server`) | OK, 37 инструментов |

`claude mcp list` иногда показывает ✘ из-за 30-секундного таймаута холодного старта (npx/uvx) — не поломка. Для uvx нужен `UV_LINK_MODE=copy` (hardlink на Android запрещён).

## Плагины
quantitative-trading 1.2.3 (wshobson: quant-analyst, risk-manager — промпты используются в зале), equity-research 0.1.2 (промпт initiating-coverage — в fundamentals), financial-analysis 0.1.1 (платные коннекторы daloopa/morningstar/factset/… выключены: `.mcp.json` плагина заменён на пустой, оригинал `.mcp.json.paid.bak`; после обновления плагина отключить снова), finance-skills (himself65, `npx plugins add`).

## Не установлено / отклонения
- torch, transformers, FinBERT — по решению владельца (OOM, сигнал 9): сентимент новостей делает Gemini по заголовкам (DECISIONS #16).
- vectorbt 1.1.2 встал и работает (numba JIT при первом запуске ≈2.5 мин) — фоллбек на pandas не понадобился.
- TA-Lib: C-библиотека собрана `make -j1` в `~/.local` (параллельная сборка убивалась Android), python-обёртка ta-lib 0.8.1.
- Vibe-Trading склонирован справочно (vendor/), в коде не используется.
