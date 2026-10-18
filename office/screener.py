"""Сканер рынка без нейросетей. Каждый проход проверяет ВЕСЬ доступный список Alpaca: все USD-пары крипты и все торгуемые акции/ETF, прошедшие фильтр оборота.
Тяжёлая часть — недельный кэш ликвидного универсума (снапшоты Alpaca по всем акциям/ETF → оборот ≥ порога по 20 дневным барам); каждый проход — дневные бары по ликвидному
универсуму (TA-Lib): оценка «тренд» (моментум, объём, пробой) и оценка «разворот» (RSI, расстояние до минимума 20 дн. и до средней). Итог — 10–20 кандидатов для отделов:
до 8 по тренду, до 8 по развороту и крупная крипта (топ по обороту). Акции — только в часы биржи. Ни одного вызова LLM."""
import json
import time

import numpy as np
import pandas as pd
import requests
import talib as ta

from . import config, data, lab

CACHE = config.STATE_DIR / "data_cache" / "universe.json"
VOL_CACHE = config.STATE_DIR / "data_cache" / "crypto_volume.json"
EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
SNAP_MIN_TURNOVER = 3_000_000.0   # грубый предфильтр по двум последним дням (IEX); точный — по 20 дневным барам
N_TREND, N_REVERSAL, N_MAX, N_MIN, N_MAJORS = 8, 8, 20, 10, 5


def _alpaca_headers() -> dict:
    import os
    return {"APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_KEY_ID", ""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("ALPACA_SECRET", "")}


def stock_assets(alpaca_client) -> list[str]:
    """Все торгуемые акции и ETF Alpaca на основных биржах (без варрантов/классов с точкой)."""
    st = alpaca_client._req("GET", "/assets", params={"asset_class": "us_equity", "status": "active"})
    return sorted(a["symbol"] for a in st if a.get("tradable") and a.get("exchange") in EXCHANGES and a["symbol"].isalpha() and len(a["symbol"]) <= 5)


def snapshot_turnover(symbols: list[str]) -> dict[str, float]:
    """Оборот $ по снапшотам Alpaca (среднее двух последних дневных баров), пачками по 800 символов."""
    out = {}
    for i in range(0, len(symbols), 800):
        r = requests.get("https://data.alpaca.markets/v2/stocks/snapshots", params={"symbols": ",".join(symbols[i:i + 800]), "feed": "iex"}, headers=_alpaca_headers(), timeout=60)
        r.raise_for_status()
        for sym, sn in r.json().items():
            bars = [b for b in (sn.get("dailyBar"), sn.get("prevDailyBar")) if b]
            if bars:
                out[sym] = float(np.mean([b["c"] * b["v"] for b in bars]))
    return out


def universe(alpaca_client, max_age_h: float = 24 * 7) -> dict:
    """{'crypto': [...BTC/USD], 'stock': [...акции и ETF с оборотом ≥ порога]}; кэш data_cache/universe.json (неделя, при сбое — старый кэш)."""
    if CACHE.exists() and time.time() - CACHE.stat().st_mtime < max_age_h * 3600:
        try:
            return _clean(json.loads(CACHE.read_text()))
        except ValueError:  # битый/пустой кэш — перекачиваем
            pass
    try:
        cr = alpaca_client._req("GET", "/assets", params={"asset_class": "crypto", "status": "active"})
        crypto = sorted(a["symbol"] for a in cr if a["symbol"].endswith("/USD") and a.get("tradable", True))
        syms = stock_assets(alpaca_client)
        turn = snapshot_turnover(syms)
        short = [s for s in syms if turn.get(s, 0) >= SNAP_MIN_TURNOVER]
        liquid_ok = []
        for i in range(0, len(short), 150):
            for sym, df in data.alpaca_bars(short[i:i + 150], "stock", days=40).items():
                if liquid(sym, df, {}):
                    liquid_ok.append(sym)
        u = {"crypto": crypto, "stock": sorted(liquid_ok)}
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(u))
        return _clean(u)
    except Exception:  # noqa: BLE001
        if CACHE.exists():
            try:
                return _clean(json.loads(CACHE.read_text()))
            except ValueError:
                pass
        raise


def _clean(u: dict) -> dict:
    """Убрать ONDO и стейблкоины из универсума."""
    return {k: [s for s in v if not config.excluded(s)] for k, v in u.items()}


def crypto_volumes(max_age_h: float = 6) -> dict[str, float]:
    """Оборот за 24 ч в $ по тикеру ('BTC') — агрегат рынка (CoinGecko, топ-250 по капитализации; запасной — Binance 24hr). Кэш в state. {} если источники недоступны."""
    if VOL_CACHE.exists() and time.time() - VOL_CACHE.stat().st_mtime < max_age_h * 3600:
        try:
            return json.loads(VOL_CACHE.read_text())
        except ValueError:
            pass
    out: dict[str, float] = {}
    try:
        for page in (1, 2):
            rows = data._get("https://api.coingecko.com/api/v3/coins/markets", params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page}).json()
            for r in rows:
                out.setdefault(r["symbol"].upper(), float(r.get("total_volume") or 0))
    except Exception:  # noqa: BLE001
        pass
    if not out:
        try:
            for r in data._get("https://data-api.binance.vision/api/v3/ticker/24hr").json():
                if r["symbol"].endswith("USDT"):
                    out[r["symbol"][:-4]] = float(r["quoteVolume"])
        except Exception:  # noqa: BLE001
            pass
    if out:
        VOL_CACHE.parent.mkdir(parents=True, exist_ok=True)
        VOL_CACHE.write_text(json.dumps(out))
        return out
    try:
        return json.loads(VOL_CACHE.read_text()) if VOL_CACHE.exists() else {}
    except ValueError:
        return {}


def liquid(sym: str, df: pd.DataFrame | None, cvol: dict[str, float]) -> bool:
    """Фильтр по обороту. Крипта: 24 ч-оборот рынка ≥ порога (нет данных об обороте — только ядро). Акции: средний дневной оборот за 20 дней (close·volume) ≥ порога."""
    if "/" in sym:
        if not cvol:
            return sym in config.CRYPTO_CORE
        return cvol.get(sym.split("/")[0], 0.0) >= config.MIN_TURNOVER["crypto"]
    return df is not None and len(df) >= 20 and float((df["close"] * df["volume"]).tail(20).mean()) >= config.MIN_TURNOVER["stock"]


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


def reversal_score(df: pd.DataFrame) -> float:
    """Оценка «разворота» по дневным барам: перепроданность (RSI), близость к минимуму 20 дней, отклонение вниз от средней 20 дней (в ATR). 0..1."""
    if len(df) < 40:
        return 0.0
    c, h, l = (df[k].to_numpy(float) for k in ("close", "high", "low"))
    atr = float(ta.ATR(h, l, c)[-1])
    if not atr or atr != atr:
        return 0.0
    rsi = float(ta.RSI(c)[-1])
    near_low = (c[-1] - l[-20:].min()) / atr
    below = (c[-20:].mean() - c[-1]) / atr
    return float(0.4 * np.clip((45 - rsi) / 25, 0, 1) + 0.3 * np.clip((1.5 - near_low) / 1.5, 0, 1) + 0.3 * np.clip(below / 3, 0, 1))


def scan(board, alpaca_client, stocks_open: bool, top_n: int = N_MAX, exclude: set | None = None, majors_out: list | None = None) -> list[dict]:
    """Кандидаты для отделов: 10–20 символов. Записывает board.candidates (score = лучшая из оценок, data: оценки и подсказка отдела). exclude — не включать (открытые позиции)."""
    u = universe(alpaca_client)
    bars = {}
    for kind, syms in (("crypto", u["crypto"]), ("stock", u["stock"] if stocks_open else [])):
        for i in range(0, len(syms), 150):
            try:
                bars.update(data.alpaca_bars(syms[i:i + 150], kind, days=100))
            except Exception:  # noqa: BLE001 — пачка не пришла: остальные считаем
                continue
    appr = lab.live_symbols(board)
    cvol = crypto_volumes()
    rows = []
    for sym, df in bars.items():
        if config.excluded(sym) or not liquid(sym, df, cvol):
            continue
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
        rows.append({"symbol": sym, "trend": score(m, lab_long), "reversal": reversal_score(df), "lab_long": lab_long, **m})
    skip = exclude or set()
    rows = [r for r in rows if r["symbol"] not in skip]
    # крупная крипта: топ по обороту (рынок), а не по сигналам
    crypto_rows = sorted((r for r in rows if "/" in r["symbol"]), key=lambda r: -cvol.get(r["symbol"].split("/")[0], 0.0))
    majors = [r["symbol"] for r in crypto_rows[:N_MAJORS]] or list(config.CRYPTO_CORE)
    if majors_out is not None:
        majors_out[:] = majors
    pick: dict[str, dict] = {}
    for key, n in (("trend", N_TREND), ("reversal", N_REVERSAL)):
        for r in sorted(rows, key=lambda r: -r[key])[:n]:
            pick.setdefault(r["symbol"], {**r, "why": key})
    for r in crypto_rows[:N_MAJORS]:
        pick.setdefault(r["symbol"], {**r, "why": "majors"})
    for r in sorted(rows, key=lambda r: -r["trend"]):                       # добор до минимума
        if len(pick) >= N_MIN:
            break
        pick.setdefault(r["symbol"], {**r, "why": "trend"})
    top = sorted(pick.values(), key=lambda r: -max(r["trend"], r["reversal"]))[:max(top_n, N_MIN)]
    ts = pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")
    board.x("DELETE FROM candidates")
    for i, r in enumerate(top, 1):
        r["score"] = max(r["trend"], r["reversal"])
        board.x("INSERT INTO candidates(ts,symbol,score,rank,data) VALUES(?,?,?,?,?)", (ts, r["symbol"], r["score"], i, json.dumps(r)))
    return top
