# trading_office — мультиагентная торговая «фирма» (paper only)

11 ролей-субагентов (`.claude/agents/`) → RiskGate в коде → Alpaca Paper. Подробности: `PLAN.md`, `DECISIONS.md`, `reports/`.

```
python -m office once [--auto] [--only BTC,AAPL]   # один цикл
python -m office run --interval 4 [--auto]         # цикл каждые N часов
python -m office watch                             # без LLM: сверка стопов/тейков (cron)
python -m office status | stop | resume | accuracy
python -m office.backtest --months 6               # reports/backtest.md
python -m pytest -q tests
```
Управление в Termux: `./trading_office.sh start|stop|status|once|watch|halt|resume|log`. Секреты — в `.env` (см. `.env.example`).
