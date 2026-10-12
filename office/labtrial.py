"""Испытательный срок стратегий лаборатории (3.5). Стратегия, прошедшая approved И поправку на множественное тестирование, 14 дней торгуется
в теневом портфеле (forward-тест на новых барах, с издержками). Голос в зале — только после подтверждения вживую:
доход после издержек > 0, не менее 3 сделок, просадка не хуже, чем у buy&hold за тот же период. Иначе статус failed (повторное испытание — не раньше чем через 30 дней)."""
import datetime as dt
import json

import numpy as np

from . import lab
from .config import COSTS, make_asset

TRIAL_DAYS, MIN_LIVE_TRADES, MAX_TRIALS, RETRY_DAYS = 14, 3, 15, 30


def _now():
    return dt.datetime.now(dt.timezone.utc)


def forward(df, family: str, params: dict, start, fee: float) -> dict:
    """Симуляция на барах с момента start: вход по сигналу входа (стартуем вне позиции), выход по сигналу выхода или стопу sl (по low бара)."""
    ent, ext, ps = lab.grid(df["close"], df["high"], df["low"], family)
    base = {k: v for k, v in params.items() if k != "sl"}
    k = next((i for i, p in enumerate(ps) if p == base), None)
    if k is None:
        return {"ret": 0.0, "trades": 0, "dd": 0.0, "bh_ret": 0.0, "bh_dd": 0.0, "bars": 0, "error": "параметры не найдены"}
    sl = params.get("sl")
    e, x = ent[k].to_numpy(), ext[k].to_numpy()
    c, lo = df["close"].to_numpy(float), df["low"].to_numpy(float)
    idx = np.flatnonzero(df.index >= start)
    if len(idx) < 2:
        return {"ret": 0.0, "trades": 0, "dd": 0.0, "bh_ret": 0.0, "bh_dd": 0.0, "bars": len(idx)}
    eq, peak, dd, trades, pos, entry = 1.0, 1.0, 0.0, 0, False, 0.0
    for i in idx[1:]:
        if pos:
            if sl and lo[i] <= entry * (1 - sl):
                eq *= (entry * (1 - sl) / c[i - 1]) * (1 - fee)
                pos = False
            elif x[i]:
                eq *= (c[i] / c[i - 1]) * (1 - fee)
                pos = False
            else:
                eq *= c[i] / c[i - 1]
        elif e[i]:
            eq *= 1 - fee
            pos, entry = True, c[i]
            trades += 1
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
    seg = c[idx]
    bh = seg / seg[0]
    bh_dd = float((bh / np.maximum.accumulate(bh) - 1).min())
    return {"ret": eq - 1, "trades": trades, "dd": dd, "bh_ret": float(bh[-1] - 1), "bh_dd": bh_dd, "bars": len(idx)}


def verdict(r: dict) -> tuple[bool, str]:
    if r.get("error"):
        return False, r["error"]
    if r["trades"] < MIN_LIVE_TRADES:
        return False, f"мало сделок за срок ({r['trades']})"
    if r["ret"] <= 0:
        return False, f"доход после издержек {r['ret']:.2%} не положителен"
    if abs(r["dd"]) > abs(r["bh_dd"]) + 1e-9:
        return False, "просадка хуже, чем у удержания"
    return True, "подтверждена вживую"


def promote(board, fetch=None, now=None) -> dict:
    """Один проход: поправка → новые испытания → оценка созревших. fetch(symbol, tf, days) — загрузка баров (по умолчанию lab.history)."""
    fetch = fetch or lab.history
    now = now or _now()
    lab.multitest(board)
    out = {"new": 0, "live": 0, "failed": 0}
    cands = board.q("SELECT * FROM strategies WHERE approved=1 AND mt=1 ORDER BY oos_sharpe - bh_sharpe DESC")
    active = board.q("SELECT COUNT(*) n FROM lab_trial WHERE status='trial'")[0]["n"]
    for s in cands:
        key = f"{s['symbol']}|{s['name']}|{s.get('tf') or '1h'}"
        t = board.q("SELECT * FROM lab_trial WHERE key=?", (key,))
        if t:
            t = t[0]
            if t["status"] != "failed" or (now - dt.datetime.fromisoformat(t["updated"])).days < RETRY_DAYS:
                continue
        if active >= MAX_TRIALS:
            break
        board.x("INSERT OR REPLACE INTO lab_trial(key,symbol,name,tf,params,start,status,updated) VALUES(?,?,?,?,?,?, 'trial', ?)",
                (key, s["symbol"], s["name"], s.get("tf") or "1h", s["params"], now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")))
        active += 1
        out["new"] += 1
    for t in board.q("SELECT * FROM lab_trial WHERE status='trial'"):
        start = dt.datetime.fromisoformat(t["start"])
        if (now - start).days < TRIAL_DAYS:
            board.x("UPDATE lab_trial SET updated=? WHERE key=?", (now.isoformat(timespec="seconds"), t["key"]))
            continue
        a = make_asset(t["symbol"])
        try:
            df = fetch(t["symbol"], t["tf"], lab.SIGNAL_D[t["tf"]] + TRIAL_DAYS + 5)
            r = forward(df, t["name"], json.loads(t["params"]), start, COSTS[a.kind].fee + COSTS[a.kind].slippage)
        except Exception:  # noqa: BLE001 — нет данных: оценим в следующий проход
            continue
        ok, note = verdict(r)
        board.x("UPDATE lab_trial SET status=?, live_ret=?, bh_ret=?, live_trades=?, live_dd=?, bh_dd=?, updated=?, note=? WHERE key=?",
                ("live" if ok else "failed", r["ret"], r["bh_ret"], r["trades"], r["dd"], r["bh_dd"], now.isoformat(timespec="seconds"), note, t["key"]))
        out["live" if ok else "failed"] += 1
    return out
