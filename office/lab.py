"""Лаборатория (фоном): vectorbt-перебор стратегий + walk-forward (train 4 мес / test 2 мес, шаг 2 мес).
Таймфреймы 15м / 1ч / 4ч; крипта и акции считаются отдельно (свои данные, комиссии, годовая база).
approved <=> OOS Sharpe > buy&hold И |OOS maxDD| < |maxDD buy&hold| И >=30 сделок OOS (+ OOS доходность > 0, защита от множественных сравнений).
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
TIMEFRAMES = ("15m", "1h", "4h")
HISTORY_D = {"15m": 500, "1h": 730, "4h": 730}   # глубина истории для walk-forward
SIGNAL_D = {"15m": 30, "1h": 60, "4h": 120}      # окно для текущего сигнала (прогрев индикаторов)
STOPS = (np.nan, 0.02, 0.05)                      # защитный стоп-лосс как параметр сетки (nan = без стопа)
FAMILIES = ("sma_cross", "rsi_revert", "donchian", "macd", "bb_revert", "bb_breakout", "adx_trend", "stoch_revert",
            "atr_breakout", "roc_momentum", "ema_pullback")


def _cross_up(a, b):
    return (a > b) & (np.roll(a, 1) <= np.roll(b, 1))


def _cross_dn(a, b):
    return (a < b) & (np.roll(a, 1) >= np.roll(b, 1))


def grid(close: pd.Series, high: pd.Series, low: pd.Series, family: str) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """Сигналы (каузальные, считаются на всей истории) для сетки параметров: колонки = комбинации."""
    c, h, lw = (x.to_numpy(float) for x in (close, high, low))
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
                    add({"fast": f, "slow": s}, _cross_up(a, b), _cross_dn(a, b))
    elif family == "rsi_revert":
        for n in (7, 14):
            r = ta.RSI(c, n)
            for lo in (25, 30, 35):
                for hi in (50, 60, 70):
                    add({"n": n, "lo": lo, "hi": hi}, r < lo, r > hi)
    elif family == "donchian":
        for n in (24, 72, 168):
            up = pd.Series(h).rolling(n).max().shift(1).to_numpy()
            dn = pd.Series(lw).rolling(n // 2).min().shift(1).to_numpy()
            add({"n": n}, c > up, c < dn)
    elif family == "macd":
        for fast, slow, sig in ((12, 26, 9), (24, 52, 18), (6, 13, 5)):
            m, s, _ = ta.MACD(c, fast, slow, sig)
            add({"fast": fast, "slow": slow, "sig": sig}, _cross_up(m, s), _cross_dn(m, s))
    elif family == "bb_revert":  # покупка у нижней полосы, выход к средней
        for n in (20, 50):
            for k in (2.0, 2.5):
                u, mid, lo = ta.BBANDS(c, n, k, k)
                add({"n": n, "k": k}, c < lo, c > mid)
    elif family == "bb_breakout":  # пробой верхней полосы, выход под среднюю
        for n in (20, 50):
            for k in (2.0, 2.5):
                u, mid, lo = ta.BBANDS(c, n, k, k)
                add({"n": n, "k": k}, c > u, c < mid)
    elif family == "adx_trend":  # тренд силён (ADX) и +DI > -DI
        for n in (14, 28):
            adx, pdi, mdi = ta.ADX(h, lw, c, n), ta.PLUS_DI(h, lw, c, n), ta.MINUS_DI(h, lw, c, n)
            for th in (20, 25, 30):
                add({"n": n, "th": th}, (adx > th) & (pdi > mdi), pdi < mdi)
    elif family == "stoch_revert":
        k, d = ta.STOCH(h, lw, c)[0], ta.STOCH(h, lw, c)[1]
        for lo in (15, 20, 25):
            for hi in (70, 80):
                add({"lo": lo, "hi": hi}, _cross_up(k, d) & (k < lo + 10), k > hi)
    elif family == "atr_breakout":  # канал Кельтнера: SMA + k·ATR
        for n in (20, 50):
            for kk in (1.5, 2.5):
                mid, atr = ta.SMA(c, n), ta.ATR(h, lw, c, n)
                add({"n": n, "k": kk}, c > mid + kk * atr, c < mid)
    elif family == "roc_momentum":
        for n in (12, 48, 120):
            for th in (2.0, 5.0):
                roc = ta.ROC(c, n)
                add({"n": n, "th": th}, roc > th, roc < 0)
    elif family == "ema_pullback":  # откат в восходящем тренде: выше EMA200 и RSI просел
        e200 = ta.EMA(c, 200)
        r = ta.RSI(c, 14)
        for lo in (35, 40, 45):
            add({"lo": lo}, (c > e200) & (r < lo), (r > 60) | (c < e200))
    idx = close.index
    return pd.DataFrame(ent, index=idx), pd.DataFrame(ext, index=idx), params


def _bars_per_year(idx: pd.DatetimeIndex) -> float:
    days = max((idx[-1] - idx[0]).days, 1)
    return len(idx) / days * 365


def _sim(close, e, x, fee, sl):
    import vectorbt as vbt
    return vbt.Portfolio.from_signals(close, e, x, fees=fee, init_cash=1.0, freq="1h",  # только для ранжирования комбинаций; метрики OOS считаются отдельно
                                      
                                      sl_stop=np.asarray(sl, float).reshape(1, -1))


def _sharpe(r: pd.Series, ppy: float) -> float:
    s = r.std()
    return float(r.mean() / s * np.sqrt(ppy)) if s and s == s else 0.0


def _maxdd(r: pd.Series) -> float:
    eq = (1 + r).cumprod()
    return float((eq / eq.cummax() - 1).min())


def _expand(ent, ext, params):
    """Сетка × защитные стопы: колонка = (комбинация сигналов, стоп)."""
    cols_e, cols_x, ps, sls = {}, {}, [], []
    for i, p in enumerate(params):
        for sl in STOPS:
            k = len(ps)
            cols_e[k], cols_x[k] = ent[i], ext[i]
            ps.append({**p, "sl": None if sl != sl else sl})
            sls.append(sl)
    return pd.DataFrame(cols_e, index=ent.index), pd.DataFrame(cols_x, index=ent.index), ps, np.array(sls)


def walk_forward(df: pd.DataFrame, family: str, fee: float) -> dict | None:
    """Метрики OOS и текущие параметры (лучшие на последнем train-окне) или None, если мало данных."""
    close = df["close"]
    ent, ext, params = grid(close, df["high"], df["low"], family)
    if not params:
        return None
    ent, ext, params, sls = _expand(ent, ext, params)
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
        pf_tr = _sim(close[tr], ent[tr], ext[tr], fee, sls)
        sh = pf_tr.sharpe_ratio().fillna(-9)
        sh[pf_tr.trades.count() < 3] = -9
        k = int(sh.idxmax())  # параметры выбираются ТОЛЬКО по train-окну
        pf_te = _sim(close[te], ent[te][[k]], ext[te][[k]], fee, sls[[k]])
        oos.append(pf_te.returns().squeeze())
        bh.append(close[te].pct_change().fillna(0))
        trades += int(pf_te.trades.count().squeeze())
    r, b = pd.concat(oos), pd.concat(bh)
    last_tr = slice(end - pd.Timedelta(days=TRAIN_D), end)  # текущие параметры: лучшие на последних 4 мес
    pf_l = _sim(close[last_tr], ent[last_tr], ext[last_tr], fee, sls)
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


def history(symbol: str, tf: str = "1h", days: int | None = None) -> pd.DataFrame:
    """Бары нужного таймфрейма. Крипта — Binance (все 3 ТФ), акции — Alpaca IEX (15м), Yahoo (1ч; 4ч = ресемплинг 1ч)."""
    a = make_asset(symbol)
    days = days or HISTORY_D[tf]
    if a.kind == "crypto":
        return data.crypto_candles(a.ticker, tf, start_ms=int((time.time() - days * 86400) * 1000))
    if tf == "15m":
        return data.alpaca_bars([a.alpaca], "stock", days=days, timeframe="15Min").get(a.alpaca, pd.DataFrame())
    h1 = data.stock_candles(a.ticker, "1h", "2y")
    if tf == "1h":
        return h1
    return h1.resample("4h", origin="start").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()


def test_symbol(board, symbol: str, tfs=TIMEFRAMES) -> list[dict]:
    a = make_asset(symbol)
    fee = COSTS[a.kind].fee + COSTS[a.kind].slippage
    out = []
    for tf in tfs:
        try:
            df = history(symbol, tf)
        except Exception:  # noqa: BLE001 — нет данных по ТФ: пропускаем только его
            df = pd.DataFrame()
        if len(df) < 500 or (df.index[-1] - df.index[0]).days < 300:
            board.x("INSERT INTO strategies(ts,symbol,name,params,approved,trades,tf,kind) VALUES(datetime('now'),?,'nodata','{}',0,0,?,?)", (symbol, tf, a.kind))
            continue
        for fam in FAMILIES:
            r = walk_forward(df, fam, fee)
            if not r:
                continue
            r["tf"] = tf
            board.x("INSERT INTO strategies(ts,symbol,name,params,approved,oos_sharpe,bh_sharpe,oos_dd,bh_dd,trades,data,tf,kind) VALUES(datetime('now'),?,?,?,?,?,?,?,?,?,?,?,?)",
                    (symbol, fam, json.dumps(r["params"]), int(r["approved"]), r["oos_sharpe"], r["bh_sharpe"], r["oos_dd"], r["bh_dd"], r["trades"], json.dumps(r), tf, a.kind))
            out.append(r)
    return out


def approved(board, symbol: str, max_age_days: int = 30) -> list[dict]:
    rows = board.q("SELECT * FROM strategies WHERE symbol=? AND approved=1 AND ts>=datetime('now',?) ORDER BY id DESC", (symbol, f"-{max_age_days} days"))
    seen, out = set(), []
    for r in rows:
        key = (r["name"], r.get("tf") or "1h")
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def state_now(df: pd.DataFrame, family: str, params: dict) -> bool:
    """Находится ли стратегия сейчас в лонге (по последним сигналам вход/выход; стоп-лосс в оценке состояния не учитывается)."""
    ent, ext, ps = grid(df["close"], df["high"], df["low"], family)
    base = {k: v for k, v in params.items() if k != "sl"}
    k = next((i for i, p in enumerate(ps) if p == base), None)
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
    dfs, longs = {}, []
    for s in strats:
        tf = s.get("tf") or "1h"
        if tf not in dfs:
            dfs[tf] = history(symbol, tf, SIGNAL_D[tf])
        longs.append((f"{s['name']}/{tf}", state_now(dfs[tf], s["name"], json.loads(s["params"]))))
    n_long = sum(1 for _, v in longs if v)
    stance = "buy" if n_long * 2 > len(longs) else "hold"
    best = max(strats, key=lambda s: s["oos_sharpe"])
    conf = round(min(0.8, 0.5 + 0.1 * max(0.0, best["oos_sharpe"] - best["bh_sharpe"])), 2) if stance == "buy" else 0.5
    desc = ", ".join(f"{n}={'long' if v else 'flat'}" for n, v in longs)
    return {"stance": stance, "confidence": conf, "rationale": f"approved стратегии: {desc} (OOS Sharpe {best['oos_sharpe']:.2f} vs B&H {best['bh_sharpe']:.2f})"}


def stats(board, hours: int = 24) -> dict:
    """Сводка лаборатории отдельно по крипте и акциям (для Telegram)."""
    out = {}
    for kind in ("crypto", "stock"):
        r = board.q("SELECT COUNT(*) n, COALESCE(SUM(approved),0) ok, COUNT(DISTINCT symbol) syms FROM strategies WHERE name!='nodata' AND COALESCE(kind,'stock')=? AND ts>=datetime('now',?)",
                    (kind, f"-{hours} hours"))[0]
        out[kind] = r
    return out
