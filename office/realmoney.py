"""Критерии перехода на реальные деньги (5.2) — ТОЛЬКО отчёт, реальная торговля не включается никогда. Условия (все сразу):
не менее 30 дней наблюдения, не менее 20 закрытых сделок (частичные фиксации не считаются), доходность офиса выше удержания S&P 500 и биткоина за тот же период,
максимальная просадка офиса меньше 10%. Когда выполнены — одно сообщение с цифрами (повтор раз в неделю, пока условия держатся); решение принимает человек."""
import datetime as dt

from .report import num, pct

DAYS, TRADES, MAX_DD = 30, 20, 0.10


def progress(board, journal, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = board.q("SELECT ts, equity FROM shadow_eq WHERE name='office' ORDER BY ts")
    ser = {n: board.q("SELECT equity FROM shadow_eq WHERE name=? ORDER BY ts", (n,)) for n in ("spy", "btc")}
    trades = journal.q("SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl FROM trades WHERE exit_reason!='partial'")[0]
    if not rows:
        return {"days": 0, "trades": trades["n"], "office": None, "spy": None, "btc": None, "max_dd": None, "ok": {}, "all_ok": False}
    eq = [r["equity"] for r in rows]
    t0 = dt.datetime.fromisoformat(rows[0]["ts"])
    peak, dd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        dd = min(dd, e / peak - 1)
    ret = {"office": eq[-1] / eq[0] - 1, **{k: (v[-1]["equity"] / v[0]["equity"] - 1 if v else None) for k, v in ser.items()}}
    days = (now - t0).total_seconds() / 86400
    ok = {"days": days >= DAYS, "trades": trades["n"] >= TRADES, "beats_spy": ret["spy"] is not None and ret["office"] > ret["spy"],
          "beats_btc": ret["btc"] is not None and ret["office"] > ret["btc"], "drawdown": abs(dd) < MAX_DD}
    return {"days": days, "trades": trades["n"], "pnl": trades["pnl"], "office": ret["office"], "spy": ret["spy"], "btc": ret["btc"], "max_dd": dd, "ok": ok, "all_ok": all(ok.values())}


def progress_line(p: dict) -> str:
    if p["office"] is None:
        return "Путь к реальным деньгам: наблюдение только началось."
    d = int(p["days"])
    mark = lambda k: "✓" if p["ok"][k] else "✗"  # noqa: E731
    return (f"Путь к реальным деньгам: {d} из {DAYS} дней {mark('days')}; сделок {p['trades']} из {TRADES} {mark('trades')}; "
            f"доходность офиса {pct(p['office'], 2)} против S&P 500 {pct(p['spy'] or 0, 2)} {mark('beats_spy')} и биткоина {pct(p['btc'] or 0, 2)} {mark('beats_btc')}; "
            f"просадка {num(abs(p['max_dd']) * 100, 1)}% (нужно меньше {num(MAX_DD * 100)}%) {mark('drawdown')}")


def final_message(p: dict) -> str:
    return ("💰 Условия для перехода на реальные деньги выполнены.\n" + progress_line(p) +
            f"\nЗакрыто сделок: {p['trades']}, итог по ним {num(p.get('pnl', 0))} $.\n"
            "Это только отчёт: офис работает на демо-счёте и реальную торговлю сам не включит. Решение принимаете вы.")


def check(board, journal, notify, kv, now: dt.datetime | None = None) -> dict:
    """Ежедневная проверка: при выполнении всех условий — сообщение (не чаще раза в 7 дней)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    p = progress(board, journal, now)
    if p["all_ok"]:
        last = kv.kv_get("realmoney_sent")
        if not last or (now - dt.datetime.fromisoformat(last)).days >= 7:
            kv.kv_set("realmoney_sent", now.isoformat())
            notify(final_message(p))
    return p
