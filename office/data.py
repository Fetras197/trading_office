"""Рыночные данные. Только публичные keyless-эндпоинты, всё best-effort кроме свечей."""
import os
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

import pandas as pd
import requests

BINANCE = "https://data-api.binance.vision/api/v3/klines"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
HL = "https://api.hyperliquid.xyz/info"
UA = {"User-Agent": "Mozilla/5.0"}
_IV_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def _get(url, **kw):
    for i in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=20, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if i == 2:
                raise
            time.sleep(25 if getattr(getattr(e, "response", None), "status_code", 0) == 429 else 1.5 * (i + 1))  # 429 CoinGecko: ждём минутное окно


def crypto_candles(symbol: str, interval: str = "1h", start_ms: int | None = None,
                   end_ms: int | None = None, limit: int = 500) -> pd.DataFrame:
    """Свечи Binance. Без start_ms — последние `limit`. С start_ms — пагинация до end_ms."""
    rows, cur = [], start_ms
    while True:
        p = {"symbol": symbol, "interval": interval, "limit": 1000 if start_ms else limit}
        if cur is not None:
            p["startTime"] = cur
        if end_ms:
            p["endTime"] = end_ms
        batch = _get(BINANCE, params=p).json()
        rows += batch
        if not start_ms or len(batch) < 1000:
            break
        cur = batch[-1][0] + _IV_MS[interval]
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "ct", "qv", "n", "tb", "tq", "i"])
    return _norm(df, "ts", "ms")


def stock_candles(ticker: str, interval: str = "1h", rng: str = "60d") -> pd.DataFrame:
    j = _get(YAHOO.format(ticker), params={"range": rng, "interval": interval}).json()
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"ts": res["timestamp"], **{k: q[k] for k in ("open", "high", "low", "close", "volume")}})
    return _norm(df.dropna(), "ts", "s")


def _norm(df: pd.DataFrame, col: str, unit: str) -> pd.DataFrame:
    df = df.copy()
    df.index = pd.to_datetime(df[col].astype("int64"), unit=unit, utc=True)
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df[~df.index.duplicated()].sort_index()


def alpaca_bars(symbols: list[str], kind: str, days: int = 90, timeframe: str = "1Day") -> dict[str, pd.DataFrame]:
    """Бары Alpaca (alpaca-py), пачкой по многим символам. Акции — бесплатный IEX-фид. {symbol: df}."""
    import datetime as dt

    from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    key, sec = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    tf = {"1Day": TimeFrame.Day, "1Hour": TimeFrame.Hour, "15Min": TimeFrame(15, TimeFrameUnit.Minute)}[timeframe]
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    if kind == "crypto":
        bars = CryptoHistoricalDataClient(key, sec).get_crypto_bars(CryptoBarsRequest(symbol_or_symbols=symbols, timeframe=tf, start=start))
    else:
        bars = StockHistoricalDataClient(key, sec).get_stock_bars(StockBarsRequest(symbol_or_symbols=symbols, timeframe=tf, start=start, feed="iex"))
    df = bars.df
    if df.empty:
        return {}
    out = {}
    for sym, g in df.groupby(level=0):
        g = g.droplevel(0)[["open", "high", "low", "close", "volume"]].astype(float)
        out[sym] = g
    return out


def candles(asset, interval: str = "1h", **kw) -> pd.DataFrame:
    if asset.kind == "crypto":
        try:
            return crypto_candles(asset.ticker, interval, **kw)
        except Exception:  # noqa: BLE001 — пары нет на Binance: берём бары Alpaca
            tf = {"1h": "1Hour", "1d": "1Day"}[interval]
            r = alpaca_bars([asset.alpaca], "crypto", days=40 if tf == "1Hour" else 400, timeframe=tf)
            if asset.alpaca not in r:
                raise
            return r[asset.alpaca]
    return stock_candles(asset.ticker, interval, kw.get("rng", "60d" if interval != "1d" else "2y"))


def market_open(df: pd.DataFrame, now: pd.Timestamp | None = None) -> bool:
    """Акции: рынок открыт, если сейчас будний день 9:30-16:00 NY и последняя свеча свежая (отсекает праздники)."""
    now = now or pd.Timestamp.now(tz="UTC")
    ny = now.tz_convert("America/New_York")
    if ny.weekday() >= 5 or not (9.5 <= ny.hour + ny.minute / 60 < 16):
        return False
    return (now - df.index[-1]) < pd.Timedelta(hours=2)


def funding_history(coin: str, start_ms: int, end_ms: int | None = None) -> pd.DataFrame:
    """Hyperliquid funding (почасовой). Колонки: funding, premium."""
    rows, cur = [], start_ms
    while True:
        body = {"type": "fundingHistory", "coin": coin, "startTime": cur}
        if end_ms:
            body["endTime"] = end_ms
        batch = requests.post(HL, json=body, timeout=20).json()
        if not batch:
            break
        rows += batch
        if len(batch) < 500:
            break
        cur = batch[-1]["time"] + 1
    if not rows:
        return pd.DataFrame(columns=["funding", "premium"])
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["time"], unit="ms", utc=True)
    return df[["fundingRate", "premium"]].astype(float).rename(columns={"fundingRate": "funding"})


def derivs_now(coin: str) -> dict:
    """Текущие funding / OI / premium с Hyperliquid."""
    try:
        meta, ctxs = requests.post(HL, json={"type": "metaAndAssetCtxs"}, timeout=20).json()
        i = [u["name"] for u in meta["universe"]].index(coin)
        c = ctxs[i]
        return {"funding_hourly": float(c["funding"]), "open_interest": float(c["openInterest"]),
                "premium": float(c["premium"]), "mark": float(c["markPx"]),
                "day_volume_usd": float(c["dayNtlVlm"])}
    except Exception as e:  # noqa: BLE001 — best-effort
        return {"error": f"unavailable: {e}"}


def news(query: str, n: int = 8) -> list[dict]:
    """Google News RSS, best-effort."""
    try:
        r = _get(f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en")
        items = ET.fromstring(r.content).findall(".//item")[:n]
        return [{"title": i.findtext("title"), "published": i.findtext("pubDate")} for i in items]
    except Exception:  # noqa: BLE001
        return []


_FUND_CACHE: dict = {}  # asset.name -> (monotonic ts, result); при ошибке источника отдаём последнее удачное


def fundamentals(asset) -> dict:
    hit = _FUND_CACHE.get(asset.name)
    if hit and time.monotonic() - hit[0] < 1800:
        return hit[1]
    r = _fundamentals(asset)
    if "error" in r and hit:
        return hit[1]
    if "error" not in r:
        _FUND_CACHE[asset.name] = (time.monotonic(), r)
    return r


def _fundamentals(asset) -> dict:
    try:
        if asset.kind == "crypto":
            # /coins/markets ищет по тикеру (symbols=) для любой монеты, а не только для трёх с известным id; берём наибольшую капитализацию
            q = {"ids": asset.cg_id} if asset.cg_id else {"symbols": asset.hl_coin.lower()}
            rows = _get("https://api.coingecko.com/api/v3/coins/markets",
                        params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 1, "price_change_percentage": "7d,30d", **q}).json()
            if not rows:
                return {"error": "unavailable: монеты нет в CoinGecko"}
            j = rows[0]
            return {"market_cap_usd": j["market_cap"], "market_cap_rank": j["market_cap_rank"], "circulating_supply": j["circulating_supply"],
                    "max_supply": j["max_supply"], "fully_diluted_valuation": j.get("fully_diluted_valuation"),
                    "volume_24h_usd": j["total_volume"], "ath_change_pct": j["ath_change_percentage"],
                    "change_7d_pct": j.get("price_change_percentage_7d_in_currency"), "change_30d_pct": j.get("price_change_percentage_30d_in_currency")}
        import yfinance as yf  # опционально
        info = yf.Ticker(asset.ticker).info
        keys = ["marketCap", "trailingPE", "forwardPE", "priceToBook", "profitMargins", "revenueGrowth",
                "debtToEquity", "returnOnEquity", "freeCashflow", "recommendationMean"]
        return {k: info.get(k) for k in keys}
    except Exception as e:  # noqa: BLE001
        return {"error": f"unavailable: {type(e).__name__}"}


def yf_news(ticker: str, n: int = 8) -> list[dict]:
    try:
        import yfinance as yf
        out = []
        for it in (yf.Ticker(ticker).news or [])[:n]:
            c = it.get("content", it)
            out.append({"title": c.get("title"), "published": c.get("pubDate") or c.get("providerPublishTime")})
        return [o for o in out if o["title"]]
    except Exception:  # noqa: BLE001
        return []


def tradingview(asset) -> dict:
    """Сводка TradingView (движок tradingview-mcp = tradingview_ta): рекомендации осцилляторов/MA на 1h и 4h."""
    from tradingview_ta import Interval, TA_Handler
    exch = ["COINBASE", "KRAKEN", "BITSTAMP"] if asset.kind == "crypto" else ["NASDAQ", "NYSE", "AMEX"]
    sym = asset.alpaca.replace("/", "")
    last = None
    for ex in exch:
        try:
            out = {}
            for tag, iv in (("1h", Interval.INTERVAL_1_HOUR), ("4h", Interval.INTERVAL_4_HOURS)):
                a = TA_Handler(symbol=sym, exchange=ex, screener="crypto" if asset.kind == "crypto" else "america", interval=iv).get_analysis()
                out[tag] = {"recommendation": a.summary["RECOMMENDATION"], "buy": a.summary["BUY"], "sell": a.summary["SELL"], "neutral": a.summary["NEUTRAL"]}
            return {"exchange": ex, **out}
        except Exception as e:  # noqa: BLE001
            last = e
    return {"error": f"unavailable: {type(last).__name__}"}


def bundle(asset, candles_df: pd.DataFrame | None = None) -> dict:
    """Пакет данных для живого цикла. Агентам уходит как JSON."""
    from . import indicators as ind
    df = candles_df if candles_df is not None else candles(asset, "1h")
    d4 = df.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    b = {"asset": asset.name, "kind": asset.kind, "as_of": str(df.index[-1]),
         "indicators_1h": ind.snapshot(df), "indicators_4h": ind.snapshot(d4),
         "last_candles_1h": df.tail(12).round(4).reset_index().astype({"ts": str}).to_dict("records")}
    b["news"] = (yf_news(asset.ticker) if asset.kind == "stock" else []) or news(f"{asset.alpaca.split('/')[0]} {'crypto' if asset.kind == 'crypto' else 'stock'}")
    b["fundamentals"] = fundamentals(asset)
    b["tradingview"] = tradingview(asset)
    if asset.kind == "crypto":
        b["derivatives"] = derivs_now(asset.hl_coin)
    return b
