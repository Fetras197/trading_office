"""Отчёты в Telegram (tgnotify): сделка с цепочкой «кто что сказал и на кого сослался» + сводка раз в 6 часов."""
import subprocess

from . import accuracy, telegram


def notify(text: str) -> None:
    """Через Bot API (telegram.py читает тот же конфиг, что tgnotify); при сбое — сам tgnotify."""
    if not telegram.notify(text):
        subprocess.run(["tgnotify", text[:3800]], capture_output=True, timeout=20, check=False)


def chain_text(board, cycle_id: str, asset: str) -> str:
    lines = []
    for r in board.cycle_reports(cycle_id, asset):
        refs = ", ".join(f"#{i}{'✓' if r['stances'].get(str(i)) == 'agree' else '✗'}" for i in r["refs"])
        lines.append(f"#{r['id']} {r['role']} [{r['confidence']:.2f}]: {r['verdict'][:110]}" + (f"\n   ↳ {refs}" if refs else ""))
    return "\n".join(lines)


def trade_message(board, res) -> str:
    head = {"opened": "🟢 ВХОД", "closed": "🔴 ВЫХОД"}.get(res.status, res.status)
    oid = res.order.get("order_id", "-")
    return f"{head} {res.asset} (paper) {res.detail}\nордер Alpaca: {oid}\n\nЦепочка (✓ согласен, ✗ оспорил):\n{chain_text(board, res.cycle_id, res.asset)}"


def summary(board, journal, broker, bench: dict) -> str:
    """bench: {'SPY': %, 'BTC': %} изменение с начала периода сводки (считает supervisor)."""
    eq = broker.equity()
    start = journal.kv_get("start_equity") or eq
    pos = journal.positions()
    acc = accuracy.role_accuracy(board)
    appr = board.q("SELECT symbol, name, oos_sharpe, bh_sharpe, trades FROM strategies WHERE approved=1 AND ts>=datetime('now','-1 day') ORDER BY oos_sharpe DESC LIMIT 5")
    tested = board.q("SELECT COUNT(*) n FROM strategies WHERE ts>=datetime('now','-1 day')")[0]["n"]
    rej = board.q("SELECT COUNT(*) n FROM rejected WHERE ts>=datetime('now','-6 hours')")[0]["n"]
    out = [f"📊 Сводка за 6ч  equity ${eq:,.0f} ({eq / start - 1:+.2%} с старта)",
           "бенчмарк: " + ", ".join(f"{k} {v:+.2%}" for k, v in bench.items()),
           "позиции: " + (", ".join(f"{n} {p['qty']:.4g}@{p['avg']:.2f}" for n, p in pos.items()) or "нет"),
           "точность ролей: " + (", ".join(f"{a['role']} {a['accuracy']:.0%}({a['n']})" for a in acc) or "пока нет созревших"),
           f"отклонено шиной (нет ссылок/невалидно): {rej}",
           f"лаборатория: протестировано за сутки {tested}, approved: " + (", ".join(f"{a['symbol']}/{a['name']} OOS {a['oos_sharpe']:.2f} vs B&H {a['bh_sharpe']:.2f} ({a['trades']} сд.)" for a in appr) or "нет")]
    return "\n".join(out)
