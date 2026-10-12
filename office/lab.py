"""Лаборатория (фоном): vectorbt-перебор стратегий + walk-forward (train 4 мес / test 2 мес, шаг 2 мес) на 1h-барах.
approved <=> OOS Sharpe > buy&hold И OOS maxDD меньше buy&hold И >=30 сделок OOS (+ OOS доходность > 0, защита от множественных сравнений).
Approved-стратегии пишутся в board.strategies и голосуют в зале как аналитик 'lab'."""
import json
import time

import numpy as np
import pandas as pd
import talib as ta

from . import data
from .config import COSTS, make_asset

TRAIN_D, TEST_D = 122, 61  # 4 и 2 месяца
MIN_TRADES = 30


def grid(close: pd.Series, high: pd.Series, low: pd.Series, family: str) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """Сигналы (каузальные, считаются на всей истории) для сетки параметров: колонки = комбинации."""
    c = close.to_numpy(float)
    ent, ext, params = {}, {}, []

    def add(p, e, x):
        k = len(params)
        params.append(p)
        ent[k], ext[k] = np.nan_to_num(e).astype(bool), np.nan_to_num(x).astype(bool)

    if family == "sma_cross":
        for f in (10, 20, 50):
            for s in (50, 100, 200):
                if f < s:
                    a, b = ta.SMA(c, f), ta.SMA(c, s)
                    add({"fast": f, "slow": s}, (a > b) & (np.roll(a, 1) <= np.roll(b, 1)), (a < b) & (np.roll(a, 1) >= np.roll(b, 1)))
    elif family == "rsi_revert":
        for n in (7, 14):
            r = ta.RSI(c, n)
            for lo in (25, 30, 35):
                for hi in (50, 60, 70):
                    add({"n": n, "lo": lo, "hi": hi}, r < lo, r > hi)
    elif family == "donchian":
        h, lw = high.to_numpy(float), low.to_numpy(float)
        for n in (24, 72, 168):
            up = pd.Series(h).rolling(n).max().shift(1).to_numpy()
            dn = pd.Series(lw).rolling(n // 2).min().shift(1).to_numpy()
            add({"n": n}, c > up, c < dn)
    elif family == "macd":
        for fast, slow, sig in ((12, 26, 9), (24, 52, 18), (6, 13, 5)):
            m, s, _ = ta.MACD(c, fast, slow, sig)
            add({"fast": fast, "slow": slow, "sig": sig}, (m > s) & (np.roll(m, 1) <= np.roll(s, 1)), (m < s) & (np.roll(m, 1) >= np.roll(s, 1)))
    idx = close.index
    return pd.DataFrame(ent, index=idx), pd.DataFrame(ext, index=idx), params


FAMILIES = ("sma_cross", "rsi_revert", "donchian", "macd")


def _bars_per_year(idx: pd.DatetimeIndex) -> float:
    days = max((idx[-1] - idx[0]).days, 1)
    return len(idx) / days * 365


def _sim(close, e, x, fee, freq_per_year):
    import vectorbt as vbt
    pf = vbt.Portfolio.from_signals(close, e, x, fees=fee, init_cash=1.0, freq="1h")
    return pf


def _sharpe(r: pd.Series, ppy: float) -> float:
    s = r.std()
    return float(r.mean() / s * np.sqrt(ppy)) if s and s == s else 0.0


def _maxdd(r: pd.Series) -> float:
    eq = (1 + r).cumprod()
    return float((eq / eq.cummax() - 1).min())


def walk_forward(df: pd.DataFrame, family: str, fee: float) -> dict | None:
    """Возвращает метрики OOS и текущие параметры (лучшие на последнем train-окне) или None, если мало данных."""
    close = df["close"]
    ent, ext, params = grid(close, df["high"], df["low"], family)
    if not params:
        return None
    ppy = _bars_per_year(df.index)
    t0, end = df.index[0], df.index[-1]
    folds, start = [], t0 + pd.Timedelta(days=60)  # прогрев индикаторов
    while start + pd.Timedelta(days=TRAIN_D + TEST_D) <= end:
        folds.append((start, start + pd.Timedelta(days=TRAIN_D), start + pd.Timedelta(days=TRAIN_D + TEST_D)))
        start += pd.Timedelta(days=TEST_D)
    if len(folds) < 3:
        return None
    oos, bh, trades = [], [], 0
    for a, b, c_ in folds:
        tr, te = slice(a, b), slice(b, c_)
        pf_tr = _sim(close[tr], ent[tr], ext[tr], fee, ppy)
        sh = pf_tr.sharpe_ratio().fillna(-9)
        nt = pf_tr.trades.count()
        sh[nt < 3] = -9
        k = int(sh.idxmax())
        pf_te = _sim(close[te], ent[te][[k]], ext[te][[k]], fee, ppy)
        oos.append(pf_te.returns().squeeze())
        bh.append(close[te].pct_change().fillna(0))
        trades += int(pf_te.trades.count().squeeze())
    r, b = pd.concat(oos), pd.concat(bh)
    # текущие параметры: лучшие на последних 4 мес
    last_tr = slice(end - pd.Timedelta(days=TRAIN_D), end)
    pf_l = _sim(close[last_tr], ent[last_tr], ext[last_tr], fee, ppy)
    shl = pf_l.sharpe_ratio().fillna(-9)
    shl[pf_l.trades.count() < 3] = -9
    cur = int(shl.idxmax())
    res = {"family": family, "params": params[cur], "oos_sharpe": _sharpe(r, ppy), "bh_sharpe": _sharpe(b, ppy), "oos_dd": _maxdd(r), "bh_dd": _maxdd(b),
           "oos_return": float((1 + r).prod() - 1), "bh_return": float((1 + b).prod() - 1), "trades": trades, "folds": len(folds)}
    res["approved"] = is_approved(res)
    return res


def is_approved(r: dict) -> bool:
    """OOS лучше buy&hold по Sharpe И по просадке, >=30 сделок OOS (+ OOS доходность > 0)."""
    return bool(r["oos_sharpe"] > r["bh_sharpe"] and abs(r["oos_dd"]) < abs(r["bh_dd"]) and r["trades"] >= MIN_TRADES and r["oos_return"] > 0)


def history(symbol: str, days: int = 730) -> pd.DataFrame:
    a = make_asset(symbol)
    if a.kind == "crypto":
        start = int((time.time() - days * 86400) * 1000)
        return data.crypto_candles(a.ticker, "1h", start_ms=start)
    return data.stock_candles(a.ticker, "1h", "2y")


def test_symbol(board, symbol: str) -> list[dict]:
    a = make_asset(symbol)
    df = history(symbol)
    if len(df) < 2000:
        board.x("INSERT INTO strategies(ts,symbol,name,params,approved,trades) VALUES(datetime('now'),?,'nodata','{}',0,0)", (symbol,))
        return []
    fee = COSTS[a.kind].fee + COSTS[a.kind].slippage
    out = []
    for fam in FAMILIES:
        r = walk_forward(df, fam, fee)
        if not r:
            continue
        board.x("INSERT INTO strategies(ts,symbol,name,params,approved,oos_sharpe,bh_sharpe,oos_dd,bh_dd,trades,data) VALUES(datetime('now'),?,?,?,?,?,?,?,?,?,?)",
                (symbol, fam, json.dumps(r["params"]), int(r["approved"]), r["oos_sharpe"], r["bh_sharpe"], r["oos_dd"], r["bh_dd"], r["trades"], json.dumps(r)))
        out.append(r)
    return out


def approved(board, symbol: str, max_age_days: int = 30) -> list[dict]:
    rows = board.q("SELECT * FROM strategies WHERE symbol=? AND approved=1 AND ts>=datetime('now',?) ORDER BY id DESC", (symbol, f"-{max_age_days} days"))
    seen, out = set(), []
    for r in rows:
        if r["name"] not in seen:
            seen.add(r["name"])
            out.append(r)
    return out


def state_now(df: pd.DataFrame, family: str, params: dict) -> bool:
    """Находится ли стратегия сейчас в лонге (по последним сигналам вход/выход)."""
    ent, ext, ps = grid(df["close"], df["high"], df["low"], family)
    k = next((i for i, p in enumerate(ps) if p == params), None)
    if k is None:
        return False
    e, x = ent[k].to_numpy(), ext[k].to_numpy()
    le, lx = (np.flatnonzero(e)[-1] if e.any() else -1), (np.flatnonzero(x)[-1] if x.any() else -1)
    return le > lx


def signal_for(board, symbol: str) -> dict | None:
    """Голос лаборатории для зала: None, если у актива нет approved-стратегий."""
    strats = approved(board, symbol)
    if not strats:
        return None
    df = history(symbol, 120)
    longs = [(s["name"], state_now(df, s["name"], json.loads(s["params"]))) for s in strats]
    n_long = sum(1 for _, v in longs if v)
    stance = "buy" if n_long * 2 > len(longs) else "hold"
    best = max(strats, key=lambda s: s["oos_sharpe"])
    conf = round(min(0.8, 0.5 + 0.1 * max(0.0, best["oos_sharpe"] - best["bh_sharpe"])), 2) if stance == "buy" else 0.5
    return {"stance": stance, "confidence": conf, "rationale": f"approved стратегии: {', '.join(f'{n}={"long" if v else "flat"}' for n, v in longs)} (OOS Sharpe {best['oos_sharpe']:.2f} vs B&H {best['bh_sharpe']:.2f})"}
