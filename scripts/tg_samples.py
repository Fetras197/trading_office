"""Тестовые сообщения КАЖДОГО типа в Telegram (с пометкой «Пример»): python scripts/tg_samples.py [--dry] — собирает тексты теми же функциями, что работают в офисе.
--dry — только напечатать (после вычитки), не отправлять."""
import datetime as dt
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from office import config, it, proofread, realmoney, report, views  # noqa: E402
from office.board import Board  # noqa: E402
from office.journal import Journal  # noqa: E402

config.load_env()
DRY = "--dry" in sys.argv
now = dt.datetime.now(dt.timezone.utc)
b, j = Board(), Journal()
cid = "sample"
for role, st, v in (("market", "buy", "buy: strong uptrend, price above all EMAs"), ("news", "buy", "buy: positive partnership news"), ("quant", "hold", "hold: elevated volatility"),
                    ("smartmoney", "buy", "buy: large profitable traders are long"), ("bull", "buy", "buy: trend and news"), ("bear", "sell", "sell: overbought on the short term"), ("trader", "buy", "buy: entry on pullback")):
    b.publish(cid, role, "ONDO/USD", v, 0.7, refs=[] if role in ("market", "news", "quant", "smartmoney") else [1], stances={} if role in ("market", "news", "quant", "smartmoney") else {1: "agree"}, data={"stance": st, "action": st})
b.publish(cid, "risk_manager", "ONDO/USD", "reduce: volatile", 0.5, refs=[1], stances={1: "agree"}, data={"verdict": "reduce", "rationale": "high volatility, reduce size"})
b.publish(cid, "portfolio_manager", "ONDO/USD", "approve: ok", 0.6, refs=[1], stances={1: "agree"}, data={"decision": "approve", "rationale": "risk is acceptable"})
view = {"equity": 100450.0, "cash": 84000.5, "last_equity": 100000.0, "positions": [{"symbol": "ONDOUSD", "plpc": 0.0159, "pl": 238.5, "qty": 28977, "avg": 0.51697, "price": 0.5252, "value": 15218.0}]}
pic = {"id": 1, "ts": now.isoformat(), "confidence": 0.7, "data": {"regime": "risk_off", "summary": "Рынок осторожничает перед решением ФРС: доходности растут, доллар сильный.", "main_risks": ["решение ФРС в среду", "рост доходностей"],
                                                                 "priority_assets": ["BTC/USD", "SOL/USD"], "forbidden": ["не покупать за 30 минут до решения ФРС"], "avoid_assets": [], "size_mult": 0.5, "no_new_entries": False}}
reg = {"crypto": {"regime": "trend"}, "stock": {"regime": "sideways"}}
evs = [{"ts": (now + dt.timedelta(hours=3)).isoformat(timespec="minutes"), "title": "Core CPI m/m", "ccy": "USD", "impact": "High", "kind": "macro"}]
rows = [{"name": "office", "title": "Весь офис", "ret": 0.0045, "max_dd": -0.004}, {"name": "spy", "title": "Держать S&P 500", "ret": 0.006, "max_dd": -0.01}, {"name": "btc", "title": "Держать биткоин", "ret": -0.0045, "max_dd": -0.02},
        {"name": "tech", "title": "Только технический анализ", "ret": 0.001, "max_dd": -0.003}, {"name": "lab", "title": "Только стратегии лаборатории", "ret": 0.0, "max_dd": 0.0}]
lab = {"crypto": {"n": 40, "ok": 0}, "stock": {"n": 54, "ok": 1}, "trial": 1, "live": 0, "failed": 0}
bench = {"SPY": 0.006, "BTC": -0.0045}
prog = {"days": 12, "trades": 3, "pnl": 120.0, "office": 0.0045, "spy": 0.006, "btc": -0.0045, "max_dd": -0.004, "ok": {"days": False, "trades": False, "beats_spy": False, "beats_btc": True, "drawdown": True}, "all_ok": False}
got = []
d = it.ITDept(j, got.append, battery_fn=lambda: {"pct": 18, "plugged": False}, net_fn=lambda: True, lab_alive_fn=lambda: False, lab_restart_fn=lambda: None, local_fn=lambda t: None,
              pressure_fn=lambda: {"claude": True, "gemini": False})
d.check_battery(); d.check_lab(); d.check_limits()
net = [{"v": False}]
d2 = it.ITDept(j, got.append, net_fn=lambda: net[0]["v"], now_fn=iter([1000.0, 1000.0, 2000.0, 2000.0]).__next__, local_fn=lambda t: None)
d2.kv.kv_set("it:net_down", 1000.0 - 1200)
net[0]["v"] = True
d2.check_net()
SAMPLES = [
    ("Покупка", report.trade_message(b, SimpleNamespace(status="opened", asset="ONDO/USD", cycle_id=cid, order={"price": 0.51697, "qty": 29049.61, "stop": 0.5033, "take": 0.5417, "equity": 100000}))),
    ("Продажа по стопу", report.close_message("ONDO/USD", -400, -0.026, "stop")), ("Продажа по цели", report.close_message("ONDO/USD", 720, 0.048, "take")),
    ("Продажа по команде", report.close_message("ONDO/USD", 12, 0.012, "manual")),
    ("Стоп подтянут", report.manage_message("stop", "ONDO/USD", price=0.5306, pct=0.0263, why=["безубыток"])),
    ("Половина продана", report.manage_message("partial", "ONDO/USD", pnl=95.0, price=0.5325, stop=0.5306)),
    ("Выход по времени", report.manage_message("time", "ONDO/USD", pnl=-8.0, pnl_pct=-0.001)),
    ("Сводка", report.summary_message(view, bench, 1, report.lab_line(lab) + "\n" + views.compare_line(rows))),
    ("Картина дня", views.picture_text(pic, reg)), ("Утренний брифинг", views.briefing_text(pic, reg, evs, view, {}, ["SOL/USD", "BTC/USD"])),
    ("Вечерний отчёт", views.evening_text(view, bench, [{"asset": "ONDO/USD", "pnl": 12.0, "exit_reason": "take"}], 1, rows, ["Не гнаться за ростом после новостей"], evs, lab)),
    ("Недельное собрание", views.weekly_text(rows, [{"asset": "ONDO/USD", "pnl": 12.0}], ["Не гнаться за ростом после новостей"], [{"role": "news", "n": 25, "accuracy": 0.4}],
                                             [{"text": "Понизить вес роли «Новости» с 1,00 до 0,80: точность 40% на 25 оценках"}], lab, "Неделя прошла ровно: чуть лучше биткоина, чуть хуже S&P 500.", realmoney.progress_line(prog))),
    ("Состояние (/статус)", views.status_text(view, False, reg, pic, lab, 1, bench, realmoney.progress_line(prog))), ("Позиции (/позиции)", views.positions_text(view, {"ONDO/USD": {"stop": 0.5033, "take": 0.5417}})),
    ("Срочная новость", report.urgent_message("ONDO/USD", "взлом пула ликвидности")), ("Паника", report.panic_message("рынке акций", ["падение за день −3,4%", "VIX 38"])),
    ("Пауза", "⏸ Пауза: новые покупки запрещены. Открытые позиции остаются под защитными стопами. Чтобы вернуться к работе, отправьте /продолжить."),
    ("Запуск", report.service("start")), ("Сбой актива", report.service("asset_fail", "ONDO/USD")), ("Сбой задания", report.service("task_fail", detail="цикл торгового зала")),
    ("Дневной стоп", report.service("halt_daily")), ("Реальные деньги", realmoney.final_message({**prog, "all_ok": True, "days": 31, "trades": 22, "office": 0.052, "spy": 0.021, "btc": -0.031, "max_dd": -0.043, "ok": {k: True for k in prog["ok"]}}))] + [(f"Служба IT {i + 1}", t) for i, t in enumerate(got)] + [
    ("Служба IT: лаборатория", "🔧 Лаборатория стратегий остановилась и перезапущена."),
    ("Служба IT: офис не работал", "⚠️ Офис не работал 32 мин (с 03:10 до 03:42) и снова запущен. Позиции всё это время были под защитными стопами брокера."),
    ("Сторож (cron в Termux)", "⚠️ Торговый офис не отвечал и был перезапущен сторожем. Позиции всё это время под защитными стопами брокера.")]
ONLY = {int(x) for x in next((a.split('=')[1] for a in sys.argv if a.startswith('--only=')), '').split(',') if x}
for i, (title, text) in enumerate(SAMPLES, 1):
    if ONLY and i not in ONLY:
        continue
    msg = f"🧪 Пример сообщения ({title}):\n{text}"
    if DRY:
        print(f"===== {i}. {title}\n{proofread.proofread(msg)}\n")
    else:
        report.notify(msg)
        time.sleep(1.5)
print(f"{len(SAMPLES)} сообщений", "(не отправлялись)" if DRY else "отправлено")
