"""Скринер без LLM (каждые 15 мин): все крипто-пары Alpaca (USD) + S&P 500 + NASDAQ-100. TA-Lib по дневным барам:
моментум (ROC20, RSI14), объём vs средний (SMA20), пробой 20-дневных уровней, бонус за сигнал approved-стратегии лаборатории.
Итог — top-N кандидатов в board.candidates; полный цикл зала запускается только по ним (+ открытые позиции)."""
import io
import json
import time

import numpy as np
import pandas as pd
import requests
import talib as ta

from . import data, lab
from .config import ROOT

CACHE = ROOT / "data_cache" / "universe.json"
UA = {"User-Agent": "Mozilla/5.0 (trading_office)"}
WIKI = {"sp500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol"),
        "ndx": ("https://stockanalysis.com/list/nasdaq-100-stocks/", "Symbol")}  # Википедия убрала таблицу состава NDX


def _wiki(url: str, col: str) -> list[str]:
    html = requests.get(url, headers=UA, timeout=30).text
    for t in pd.read_html(io.StringIO(html)):
        if col in t.columns:
            return [str(s).strip().replace("-", ".") for s in t[col].dropna()]
    raise ValueError("нет таблицы " + col)


def universe(alpaca_client, max_age_h: float = 24 * 7) -> dict:
    """{'crypto': [...BTC/USD], 'stock': [...]}; кэш data_cache/universe.json (обновление раз в неделю, при сбое — старый кэш)."""
    if CACHE.exists() and time.time() - CACHE.stat().st_mtime < max_age_h * 3600:
        return json.loads(CACHE.read_text())
    try:
        cr = alpaca_client._req("GET", "/assets", params={"asset_class": "crypto", "status": "active"})
        crypto = sorted(a["symbol"] for a in cr if a["symbol"].endswith("/USD") and a.get("tradable", True))
        st = alpaca_client._req("GET", "/assets", params={"asset_class": "us_equity", "status": "active"})
        ok = {a["symbol"] for a in st if a.get("tradable")}
        stocks = sorted((set(_wiki(*WIKI["sp500"])) | set(_wiki(*WIKI["ndx"]))) & ok)
        u = {"crypto": crypto, "stock": stocks}
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_text(json.dumps(u))
        return u
    except Exception:  # noqa: BLE001
        if CACHE.exists():
            return json.loads(CACHE.read_text())
        raise


def metrics(df: pd.DataFrame) -> dict | None:
    if len(df) < 40:
        return None
    c, h, l, v = (df[k].to_numpy(float) for k in ("close", "high", "low", "volume"))
    avg_v = ta.SMA(v, 20)[-2]
    return {"price": c[-1], "roc20": float(ta.ROC(c, 20)[-1]), "rsi14": float(ta.RSI(c)[-1]),
            "vol_ratio": float(v[-2:].mean() / avg_v) if avg_v and avg_v == avg_v else 1.0,
            "breakout": 1 if c[-1] > h[-21:-1].max() else (-1 if c[-1] < l[-21:-1].min() else 0)}


def score(m: dict, lab_long: bool = False) -> float:
    s = (0.4 * np.tanh(m["roc20"] / 15) + 0.2 * np.tanh(m["vol_ratio"] - 1) + 0.25 * m["breakout"]
         + (0.15 if lab_long else 0.0) - (0.2 if m["rsi14"] > 80 else 0.0))
    return float(s)


def scan(board, alpaca_client, stocks_open: bool, top_n: int = 5, exclude: set | None = None) -> list[dict]:
    u = universe(alpaca_client)
    bars = {}
    for kind, syms in (("crypto", u["crypto"]), ("stock", u["stock"] if stocks_open else [])):
        for i in range(0, len(syms), 150):
            try:
                bars.update(data.alpaca_bars(syms[i:i + 150], kind, days=100))
            except Exception:  # noqa: BLE001 — пачка не пришла: остальные считаем
                continue
    appr = lab.live_symbols(board)
    rows = []
    for sym, df in bars.items():
        m = metrics(df)
        if not m:
            continue
        lab_long = False
        if sym in appr:
            try:
                sg = lab.signal_for(board, sym)
                lab_long = bool(sg and sg["stance"] == "buy")
            except Exception:  # noqa: BLE001
                pass
        rows.append({"symbol": sym, "score": score(m, lab_long), "lab_long": lab_long, **m})
    rows.sort(key=lambda r: -r["score"])
    ts = pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")
    board.x("DELETE FROM candidates")
    top = [r for r in rows if r["symbol"] not in (exclude or set())][:top_n]
    for i, r in enumerate(top, 1):
        board.x("INSERT INTO candidates(ts,symbol,score,rank,data) VALUES(?,?,?,?,?)", (ts, r["symbol"], r["score"], i, json.dumps(r)))
    return top
