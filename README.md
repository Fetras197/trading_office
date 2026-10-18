# trading_office — «Торговый зал» (Alpaca **paper only**)

Мульти-агентная фирма на готовых компонентах (TradingAgents, ai-hedge-fund, wshobson/quantitative-trading, financial-services/equity-research,
TA-Lib, Riskfolio-Lib, PyPortfolioOpt, vectorbt, alpaca-py). Карта, команды и результаты приёмки — `reports/FINAL.md`.
Решения без вопросов — `DECISIONS.md`. Компоненты и статус установки — `reports/components.md`.

Запуск — GitHub Actions (`.github/workflows/office.yml`, `python -m office cloud`); на телефоне офис не запускается. Локально для отладки: `python -m office once SYMBOL --dry-run`, `status`, `screen`.
Секреты — `.env` (в .gitignore); образец — `.env.example`. Тесты: `.venv/bin/python -m pytest`.
