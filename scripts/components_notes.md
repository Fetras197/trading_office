
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
