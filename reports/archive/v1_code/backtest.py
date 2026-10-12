"""Бэктест 6 мес.: rule-based роли (tech+derivs) → тот же Office/RiskGate, SimBroker с комиссиями Alpaca.
LLM-роли не участвуют (нет исторических новостей/фундаментала, look-ahead модели). Параметры правил зафиксированы ДО прогона, не подгонялись."""
import argparse
import math
import pickle
from dataclasses import replace

import pandas as pd

from . import accuracy, config, data
from .brains import RuleBrain
from .broker import SimBroker
from .config import ASSETS, COSTS
from .journal import Journal
from .office import Office

CACHE = config.ROOT / "data_cache"
WARMUP_DAYS = 15
STEP = "4h"


def load(months: int):
    """Свечи 1h (с прогревом), funding. Кэш на диске."""
    CACHE.mkdir(exist_ok=True)
    end = pd.Timestamp.now(tz="UTC").floor("h")
    start = end - pd.Timedelta(days=round(months * 30.5))
    f = CACHE / f"bt_{months}_{end:%Y%m%d%H}.pkl"
    if f.exists():
        return pickle.loads(f.read_bytes()) | {"start": start, "end": end}
    candles, funding = {}, {}
    ws = int((start - pd.Timedelta(days=WARMUP_DAYS)).timestamp() * 1000)
    for a in ASSETS:
        if a.kind == "crypto":
            candles[a.name] = data.crypto_candles(a.ticker, "1h", start_ms=ws)
            funding[a.name] = data.funding_history(a.hl_coin, ws)["funding"]
        else:
            candles[a.name] = data.stock_candles(a.ticker, "1h", "1y")
    f.write_bytes(pickle.dumps({"candles": candles, "funding": funding}))
    return {"candles": candles, "funding": funding, "start": start, "end": end}


def run(months=6, slippage=None, d=None):
    d = d or load(months)
    costs = {k: replace(v, slippage=slippage) if slippage is not None else v for k, v in COSTS.items()}
    j = Journal(":memory:")
    kinds = {a.name: a.kind for a in ASSETS}
    broker = SimBroker(j, kinds, costs)
    office = Office(RuleBrain(), broker, j, mode="backtest")
    grid = pd.date_range(d["start"].ceil(STEP), d["end"], freq=STEP, tz="UTC")
    curve, prev = [], grid[0] - pd.Timedelta(hours=4)
    for ts in grid:
        prices = {}
        for a in ASSETS:
            df = d["candles"][a.name]
            hist = df[df.index < ts].tail(1000)  # бар с меткой t закрывается в t+1h → доступны только бары < ts
            if len(hist) < 210:
                continue
            prices[a.name] = hist["close"].iloc[-1]
            win = df[(df.index >= prev) & (df.index < ts)]
            if len(win):
                broker.check_exits(a.name, win["high"].max(), win["low"].min(), str(ts))
        for a in ASSETS:
            df = d["candles"][a.name]
            hist = df[df.index < ts].tail(1000)
            if len(hist) < 210 or ts - hist.index[-1] > pd.Timedelta(hours=2):
                continue  # нет свежего бара (рынок акций закрыт)
            from . import indicators as ind
            h4 = hist.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
            bundle = {"asset": a.name, "as_of": str(hist.index[-1]), "indicators_1h": ind.snapshot(hist), "indicators_4h": ind.snapshot(h4),
                      "derivatives": {}}
            if a.name in d["funding"]:
                fs = d["funding"][a.name]
                fs = fs[fs.index <= ts]
                if len(fs):
                    bundle["derivatives"] = {"funding_hourly": float(fs.iloc[-1])}
            office.step(a, bundle, str(ts), prices)
        curve.append((ts, broker.equity(prices)))
        prev = ts
    # закрываем остаток по рынку для честного PnL
    last_ts = str(grid[-1])
    for name in list(j.positions()):
        broker.sell(name, prices[name], last_ts, "end_of_test")
    curve.append((grid[-1], broker.equity(prices)))
    accuracy.settle(j, lambda n: d["candles"][n])
    return {"journal": j, "curve": pd.Series(dict(curve)), "grid": grid, "data": d}


def metrics(res):
    j, curve = res["journal"], res["curve"]
    tr = pd.DataFrame(j.q("SELECT * FROM trades"))
    eq0, eq1 = curve.iloc[0], curve.iloc[-1]
    dd = (curve / curve.cummax() - 1).min()
    daily = curve.resample("1D").last().dropna().pct_change().dropna()
    sharpe = daily.mean() / daily.std() * math.sqrt(365) if len(daily) > 5 and daily.std() > 0 else float("nan")
    m = {"start_equity": eq0, "end_equity": eq1, "return_pct": (eq1 / config.START_CASH - 1) * 100, "max_dd_pct": dd * 100, "sharpe": sharpe,
         "trades": len(tr)}
    if len(tr):
        w, l = tr[tr.pnl > 0], tr[tr.pnl <= 0]
        m |= {"winrate_pct": len(w) / len(tr) * 100, "pnl": tr.pnl.sum(), "avg_r": tr.r_mult.mean(),
              "profit_factor": w.pnl.sum() / -l.pnl.sum() if len(l) and l.pnl.sum() < 0 else float("inf"),
              "avg_win": w.pnl.mean() if len(w) else 0, "avg_loss": l.pnl.mean() if len(l) else 0}
        m["by_asset"] = tr.groupby("asset").agg(n=("pnl", "size"), winrate=("pnl", lambda s: (s > 0).mean() * 100), pnl=("pnl", "sum")).round(1)
        m["by_exit"] = tr.groupby("exit_reason").agg(n=("pnl", "size"), pnl=("pnl", "sum")).round(1)
    return m


def buy_hold(d, name):
    df = d["candles"][name]
    df = df[(df.index >= d["start"]) & (df.index <= d["end"])]
    ret = df["close"].iloc[-1] / df["close"].iloc[0] - 1
    dd = (df["close"] / df["close"].cummax() - 1).min()
    return ret * 100, dd * 100


def md(df: pd.DataFrame, index=True) -> str:
    d = df.reset_index() if index else df
    rows = ["| " + " | ".join(map(str, d.columns)) + " |", "|" + "---|" * len(d.columns)]
    rows += ["| " + " | ".join(str(v) for v in r) + " |" for r in d.itertuples(index=False)]
    return "\n".join(rows)


def _fmt(x, p=2):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:,.{p}f}"


def report(months=6) -> str:
    d = load(months)
    base = run(months, d=d)
    stress = run(months, slippage=0.002, d=d)
    mb, ms = metrics(base), metrics(stress)
    spy, btc = buy_hold(d, "SPY"), buy_hold(d, "BTC")
    acc = pd.DataFrame(accuracy.role_accuracy(base["journal"]))
    L = [f"# Бэктест {months} мес. ({d['start']:%F} → {d['end']:%F})", "",
         "> Rule-based роли (tech: EMA/MACD/RSI; derivs: Hyperliquid funding). LLM-роли НЕ участвуют: нет исторических новостей/фундаментала, "
         "модель помнит историю (look-ahead), прогон 11 ролей × 7 активов × ~1100 шагов нереалистичен по стоимости. "
         "Это базлайн и проверка риск-пайплайна, **не оценка LLM-фирмы**. Параметры правил заданы до прогона и не подгонялись.", "",
         f"Условия: старт ${config.START_CASH:,.0f}, шаг решения {STEP}, активы {', '.join(a.name for a in ASSETS)}; комиссии: крипта 0.25%, акции 0%; "
         "проскальзывание 0.05%; риск ≤2%/сделку, нотионал ≤25%, ≤3 позиции, ≤5 входов/день, дневной стоп −5%; long-only, cash, 1x.", "",
         "## Итог", "", "| Метрика | Базовый (slip 0.05%) | Стресс (slip 0.20%) | B&H SPY | B&H BTC |", "|---|---|---|---|---|",
         f"| Доходность, % | {_fmt(mb['return_pct'])} | {_fmt(ms['return_pct'])} | {_fmt(spy[0])} | {_fmt(btc[0])} |",
         f"| Макс. просадка, % | {_fmt(mb['max_dd_pct'])} | {_fmt(ms['max_dd_pct'])} | {_fmt(spy[1])} | {_fmt(btc[1])} |",
         f"| Sharpe (дневн., ×√365) | {_fmt(mb['sharpe'])} | {_fmt(ms['sharpe'])} | — | — |",
         f"| Сделок | {mb['trades']} | {ms['trades']} | — | — |",
         f"| Винрейт, % | {_fmt(mb.get('winrate_pct'))} | {_fmt(ms.get('winrate_pct'))} | — | — |",
         f"| PnL, $ | {_fmt(mb.get('pnl'), 0)} | {_fmt(ms.get('pnl'), 0)} | — | — |",
         f"| Ср. R-мультипл | {_fmt(mb.get('avg_r'))} | {_fmt(ms.get('avg_r'))} | — | — |",
         f"| Profit factor | {_fmt(mb.get('profit_factor'))} | {_fmt(ms.get('profit_factor'))} | — | — |", ""]
    if "by_asset" in mb:
        L += ["## По активам (базовый)", "", md(mb["by_asset"]), "", "## По типу выхода (базовый)", "", md(mb["by_exit"]), ""]
    L += ["## Точность по ролям (triple-barrier ±ATR·√24 за 24ч)", "",
          "Оценивались только роли, имевшие данные в истории; fundamental/news = abstain (исключены). Остальные 7 ролей (bull, bear, trader, risk, PM, executor, journal) в бэктесте не работают.", "",
          md(acc, index=False) if len(acc) else "нет данных", "",
          "`accuracy` — доля верных среди всех мнений роли (hold верен, если за 24ч цена не вышла за барьеры); `accuracy_dir` — только buy/sell (таймаут без касания барьера считается промахом); `hit_rate_dir` — среди buy/sell, где какой-то барьер был задет, доля угаданных направлений (50% = монетка).", ""]
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=6)
    a = ap.parse_args()
    out = report(a.months)
    (config.ROOT / "reports" / "backtest.md").write_text(out)
    print(out)
