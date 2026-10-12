# Компоненты (2026-10-12 06:31)

| Компонент | Версия | Статус | Проверка |
|---|---|---|---|
| TradingAgents (vendor, ядро) | 0.6.0 @ 1394a3f 2026-10-03 | OK | bull_node |
| ai-hedge-fund (Buffett/Munger/Druckenmiller) | 78b779c 2026-10-02 | OK | You are Warren Buffett, evalua |
| Vibe-Trading (справочно) | d3c7681 2026-10-12 | OK | True |
| TA-Lib | 0.8.1 | OK | 46.60454794368487 |
| alpaca-py + Alpaca paper | 0.44.0 | OK | AccountStatus.ACTIVE equity=100000 |
| riskfolio-lib (CVaR) | 7.4.0 | OK | [[0.3, 0.42, 0.27]] |
| PyPortfolioOpt | 1.6.0 | OK | OrderedDict({'a': 0.385689673183872, 'b': 0.3135352640152288, 'c': 0.3007750628008992}) |
| vectorbt | 1.1.2 | OK | -0.154 |
| yfinance | 1.7.0 | OK | 5 |
| claude -p (Claude) | cli | OK | Ok |
| Gemini REST | gemini-3.1-flash-lite | OK | ok |
| Claude plugins |  | OK | quantitative-trading, equity-research, financial-analysis |
| MCP (claude mcp list) |  | OK | plugin ✘ | github ✔ | futbin ✘ | context7 ✔ | alpaca ✔ | yfinance ✔ | coingecko ✘ | tradin |
| transformers/FinBERT | - | SKIP | решение владельца: torch не ставится (OOM) → сентимент Gemini/Claude по заголовкам, DECISIONS #16 |

## Ручной зонд MCP (stdio initialize + tools/list, `/tmp/mcpprobe.py`)
| MCP | Результат |
|---|---|
| alpaca (`uvx alpaca-mcp-server`) | OK, 72 инструмента (paper через ALPACA_PAPER_TRADE=true) |
| yfinance (`uvx --with 'mcp<2' yfinance-mcp`) | OK, 18 инструментов. Без пина `mcp<2` падает (`mcp.server.fastmcp` удалён в mcp 2.x) |
| coingecko (`npx -y @coingecko/coingecko-mcp`) | OK, 2 инструмента (execute, search_docs) |
| tradingview (`.venv/bin/tradingview-mcp`, pip `tradingview-mcp-server`) | OK, 37 инструментов |
`claude mcp list` иногда показывает ✘ из-за 30-секундного таймаута холодного старта (npx/uvx) — не поломка. Для uvx нужен `UV_LINK_MODE=copy` (hardlink на Android запрещён).

## Плагины
quantitative-trading 1.2.3 (wshobson: quant-analyst, risk-manager), equity-research 0.1.2, financial-analysis 0.1.1 (платные коннекторы daloopa/morningstar/factset/… выключены: `.mcp.json` плагина заменён на пустой, оригинал `.mcp.json.paid.bak`), finance-skills (himself65, `npx plugins add`). Платные MCP после обновления плагина придётся отключить снова.
