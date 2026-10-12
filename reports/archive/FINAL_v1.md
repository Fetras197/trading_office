# FINAL — trading_office (2026-10-12)

Статус: **собрано и протестировано на моках + частично вживую; в Termux не запускалось** (запуск только вами). 69 тестов зелёные, ruff чист.

## Что есть
- 11 субагентов `.claude/agents/*.md` (tech, fundamental, news_sentiment, onchain_derivs, bull, bear, trader, risk_manager, portfolio_manager, executor, journal).
- Пайплайн: данные (Binance public / Yahoo / Hyperliquid / Google News / CoinGecko) → 4 аналитика (Gemini) → bull/bear → trader → risk_manager → PM → RiskGate (код) → executor-проверка → [кнопки Telegram] → Alpaca Paper.
- Роли: аналитики — Gemini (REST, цепочка моделей), остальные — Claude (`claude -p`, sonnet; executor/journal — haiku).
- Журнал SQLite `office.db`: runs, opinions (каждая роль), decisions, fills, positions, trades, equity, outcomes. Точность по ролям — triple-barrier (±ATR·√24, 24ч) + shadow rule-based базлайн в каждом цикле.
- Бэктест: `reports/backtest.md`. Решения: `DECISIONS.md`. Ресерч: `reports/research.md`.

## Жёсткие правила (в коде, с тестами)
paper-only (клиент отвергает не-paper хост) · размер ≤ cash (1x) · long-only, sell только закрывает лонг, опционов нет · риск до стопа ≤2% эквити **с комиссией и проскальзыванием** · нотионал ≤25% · ≤3 позиции · ≤5 входов/сутки (UTC) · дневной стоп −5% · стоп обязателен (акции — bracket, крипта — market + защитный stop_limit; не встал → немедленное закрытие) · ошибка/невалидный ответ агента → пропуск цикла по активу · файл `STOP` = kill-switch.

## Что проверено вживую (не на моках)
- Alpaca Paper: счёт ACTIVE, $100k, запросы account/clock/positions/orders. **Crypto bracket/OTO отклоняются API** (проверено) → схема из DECISIONS #3. Ордера на исполнение вживую НЕ отправлялись (кроме лимитного теста с ценой далеко от рынка, отменён).
- Gemini: реальные вызовы; `gemini-3.5-flash` упёрся в дневную free-квоту → цепочка моделей. Claude: реальные вызовы всех 6 Claude-ролей, схемы валидны.
- Полный dry-run BTC (без ордеров): 4 аналитика → short-circuit hold (43с). Путь bull→trader живой, risk/PM/executor — живой на канонических входах.
- **Не проверялось вживую:** исполнение ордеров через AlpacaBroker (только моки), Telegram-кнопки (нет кредов в proot), alpaca-mcp-server (`uvx` в proot не запускается; конфиг в `.mcp.json`).

## Бэктест 6 мес. (rule-based базлайн, не LLM-фирма) — **убыточный**
Базовые правила (EMA/MACD/RSI + funding): −16.2% при 180 сделках, винрейт 34%, PF 0.67, просадка −16.4%; стресс (slip 0.2%) −25.1%. Buy&hold за тот же период: SPY +14.6% (DD −4.5%), BTC +14.0% (DD −29.4%). Направленные сигналы tech/derivs — на уровне монетки (hit-rate 49.5% / 42.9%). Вывод: у тривиальных правил **нет** edge; стопы 2×ATR_1h слишком часто выбивают (117 stop против 61 take). Параметры я не подгонял под результат (это было бы переобучением на тех же данных). Что LLM-слой добавит — неизвестно: измерять только на живом paper-журнале (`python -m office accuracy`, сравнение с `rule:*` shadow-мнениями). **Не включайте `--auto` на реальные деньги и не считайте бэктест доказательством прибыльности.**

## Gemini-ревью
`relay-review` упал по таймауту (gemini CLI, 600с). Заменил прямым ревью через Gemini REST (`gemini-3.5-flash-lite`, 7 модулей): 9 замечаний. Принято и исправлено: (6) комиссия крипты в получаемом активе → реальный qty берётся с брокера, PnL с комиссиями; (9) стоп не встал + закрытие не подтверждено → позиция остаётся в журнале + тревога в Telegram; (1) точный stop-ордер при сверке; (4) любая ошибка на этапе агентов → пропуск; (2, частично) file-lock против параллельных циклов. Отклонено: гонки внутри цикла (шаги последовательны), «пересчёт риска после floor» (floor только уменьшает), валидация цен (уже есть), часовые пояса (всё UTC).

## Известные ограничения
Gemini free-tier: ~170 запросов/сутки при `--interval 4` могут не поместиться → при исчерпании цепочки цикл по активу пропускается (безопасно, но молча теряются решения). Нужен платный ключ или `--interval 8`. Ключи были присланы в чате → **перевыпустите после тестов**. alpaca-mcp-server подключён только для ручной работы.

## Команды для Termux
```bash
cd ~/projects/trading_office
pkg install python-pandas -y; pip install requests pytest      # один раз (pandas/numpy в Termux ставятся через pkg)
python3 -m pytest -q tests                                       # sanity: 69 passed

./trading_office.sh once --only BTC                              # один цикл по BTC (кнопки Telegram; без кредов: добавьте --auto)
./trading_office.sh once --auto                                  # один цикл по всем, без кнопок
termux-wake-lock; INTERVAL=4 ./trading_office.sh start           # демон раз в 4ч (с кнопками); AUTO=1 INTERVAL=4 ./trading_office.sh start — без кнопок
./trading_office.sh status                                       # процесс + счёт + позиции + последние решения + точность ролей
./trading_office.sh log 100                                      # хвост office.log
./trading_office.sh halt                                         # kill-switch (STOP): новых решений нет, стопы на брокере остаются
./trading_office.sh resume                                       # снять STOP
./trading_office.sh stop                                         # остановить демон

crontab -e   # (pkg install cronie; crond) — сверка стопов/тейков без LLM каждые 15 мин + утренний статус:
*/15 * * * * cd ~/projects/trading_office && ./trading_office.sh watch >> watch.log 2>&1
python3 -m office.backtest --months 6                            # перегенерировать reports/backtest.md (~10 мин)
```
Для Telegram-кнопок положите в `.env` `TG_TOKEN`/`TG_CHAT` (лучше отдельный бот); иначе возьмётся конфиг tgnotify.
