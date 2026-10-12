"""Бесплатные источники данных исследовательского отдела (без ключей). Всё best-effort: при сбое возвращается {"error": ...} или [],
исследователь тогда пишет abstain / работает по остальным данным. Источники проверены 2026-10-12 из Termux/proot:
Yahoo chart (VIX, DXY, доходности, индексы, сектора), NY Fed (EFFR), BLS v1 (CPI, безработица), Faireconomy (календарь), Polymarket gamma,
OKX public (funding, OI, ликвидации — Binance futures/Bybit из региона недоступны), alternative.me (индекс страха и жадности)."""
import datetime as dt
import json
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from . import data

UA = {"User-Agent": "Mozilla/5.0 (trading_office)"}
_CACHE: dict[str, tuple[float, object]] = {}


def cached(key: str, ttl: float, fn):
    """Кэш в памяти процесса; при ошибке источника отдаём последнее удачное значение (если есть)."""
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as e:  # noqa: BLE001
        if hit:
            return hit[1]
        return {"error": f"unavailable: {type(e).__name__}"}
    _CACHE[key] = (time.monotonic(), val)
    return val


def _get_json(url, **kw):
    return data._get(url, **kw).json()


# ---------------- макро ----------------
YAHOO_MACRO = {"VIX": "^VIX", "DXY": "DX-Y.NYB", "US10Y": "^TNX", "US5Y": "^FVX", "US3M": "^IRX", "SPX": "^GSPC", "GOLD": "GC=F", "OIL": "CL=F"}


def yahoo_close(sym: str, rng: str = "6mo") -> pd.Series:
    return data.stock_candles(sym, "1d", rng)["close"].dropna()


def _chg(s: pd.Series, n: int) -> float | None:
    return round(float(s.iloc[-1] / s.iloc[-1 - n] - 1), 4) if len(s) > n else None


def market_levels() -> dict:
    """Уровни и изменения за 1/5/20 дней: VIX, DXY, доходности США (в %, ^TNX уже в %), S&P 500, золото, нефть."""
    out = {}
    for k, sym in YAHOO_MACRO.items():
        try:
            s = yahoo_close(sym)
            row = {"last": round(float(s.iloc[-1]), 3), "chg_1d": _chg(s, 1), "chg_5d": _chg(s, 5), "chg_20d": _chg(s, 20)}
            if k == "SPX" and len(s) >= 50:
                row["above_sma50"] = bool(s.iloc[-1] > s.tail(50).mean())
            out[k] = row
        except Exception as e:  # noqa: BLE001
            out[k] = {"error": type(e).__name__}
    if all("last" in out.get(k, {}) for k in ("US10Y", "US3M")):
        out["curve_10y_3m"] = round(out["US10Y"]["last"] - out["US3M"]["last"], 3)
    return out


def effr() -> dict:
    j = _get_json("https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json")["refRates"][0]
    return {"effr": j["percentRate"], "target_from": j["targetRateFrom"], "target_to": j["targetRateTo"], "date": j["effectiveDate"]}


def bls_macro() -> dict:
    """CPI (all items, NSA) y/y и безработица, BLS v1 без ключа (лимит 25 запросов/сутки на IP -> кэш 12 ч)."""
    r = requests.post("https://api.bls.gov/publicAPI/v1/timeseries/data/", json={"seriesid": ["CUUR0000SA0", "LNS14000000"]}, headers=UA, timeout=30).json()
    ser = {s["seriesID"]: [d for d in s["data"] if d["value"].replace(".", "", 1).isdigit()] for s in r["Results"]["series"]}  # у BLS бывают пропуски ("-")
    cpi, un = ser["CUUR0000SA0"], ser["LNS14000000"]
    vals = {(d["year"], d["period"]): float(d["value"]) for d in cpi}
    last = cpi[0]
    prev_year = vals.get((str(int(last["year"]) - 1), last["period"]))
    return {"cpi_period": f"{last['year']}-{last['period'][1:]}", "cpi_yoy": round(float(last["value"]) / prev_year - 1, 4) if prev_year else None,
            "unemployment_period": f"{un[0]['year']}-{un[0]['period'][1:]}", "unemployment": float(un[0]["value"])}


def fear_greed() -> dict:
    j = _get_json("https://api.alternative.me/fng/?limit=2")["data"]
    return {"value": int(j[0]["value"]), "label": j[0]["value_classification"], "prev": int(j[1]["value"]) if len(j) > 1 else None}


def polymarket(queries=("fed decision", "bitcoin"), per_query: int = 3, markets_per_event: int = 5) -> list[dict]:
    """Вероятности «Да» по активным рынкам предсказаний (ФРС, крипта). Сортировка по объёму торгов."""
    out = []
    for q in queries:
        j = _get_json("https://gamma-api.polymarket.com/public-search", params={"q": q, "limit_per_type": 8, "events_status": "active"})
        evs = [e for e in j.get("events", []) if e.get("active") and not e.get("closed") and e.get("markets")]
        evs.sort(key=lambda e: -float(e.get("volume") or 0))
        for e in evs[:per_query]:
            ms = []
            for m in e["markets"]:
                if m.get("closed"):
                    continue
                try:
                    p = float(json.loads(m["outcomePrices"])[0])
                except (KeyError, ValueError, TypeError, IndexError):
                    continue
                ms.append({"question": m["question"][:110], "yes": round(p, 3)})
            ms.sort(key=lambda m: -m["yes"])
            out.append({"query": q, "event": e["title"], "ends": (e.get("endDate") or "")[:10], "volume_usd": round(float(e.get("volume") or 0)), "markets": ms[:markets_per_event]})
    return out


# ---------------- крипто-деривативы (OKX public) ----------------
OKX = "https://www.okx.com/api/v5/public"


def okx_deriv(coin: str) -> dict:
    """Funding, открытый интерес и ликвидации за последние записи (OKX swap COIN-USDT-SWAP). Ликвидации: объём в USD по сторонам."""
    inst = f"{coin}-USDT-SWAP"
    f = _get_json(f"{OKX}/funding-rate", params={"instId": inst})["data"][0]
    oi = _get_json(f"{OKX}/open-interest", params={"instType": "SWAP", "instId": inst})["data"][0]
    ctv = float(_get_json(f"{OKX}/instruments", params={"instType": "SWAP", "instId": inst})["data"][0]["ctVal"])
    liq = _get_json(f"{OKX}/liquidation-orders", params={"instType": "SWAP", "uly": f"{coin}-USDT", "state": "filled"})["data"]
    long_usd = short_usd = 0.0
    t_min = None
    for blk in liq:
        for d in blk.get("details", []):
            usd = float(d["sz"]) * ctv * float(d["bkPx"])
            if d["posSide"] == "long":   # ликвидированы лонги (продажа принудительно)
                long_usd += usd
            else:
                short_usd += usd
            t = int(d["ts"])
            t_min = t if t_min is None else min(t_min, t)
    span_h = round((time.time() * 1000 - t_min) / 3_600_000, 1) if t_min else None
    return {"coin": coin, "funding_8h": float(f["fundingRate"]), "next_funding": float(f["nextFundingRate"]) if f.get("nextFundingRate") else None,
            "open_interest_usd": round(float(oi["oiUsd"])), "liq_long_usd": round(long_usd), "liq_short_usd": round(short_usd), "liq_window_h": span_h}


def okx_deriv_safe(coin: str) -> dict:
    return cached(f"okx:{coin}", 600, lambda: okx_deriv(coin))


# ---------------- календарь ----------------
FF = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json", "https://nfs.faireconomy.media/ff_calendar_nextweek.json"]
CRYPTO_NOTES = []  # событий крипты без бесплатного API нет: экспирация опционов Deribit считается в коде (deribit_expiry)


def faireconomy_events() -> list[dict]:
    out = []
    for url in FF:
        try:
            rows = _get_json(url)
        except Exception:  # noqa: BLE001 — следующая неделя может быть ещё не опубликована
            continue
        for r in rows:
            if r.get("impact") not in ("High", "Medium"):
                continue
            try:
                ts = dt.datetime.fromisoformat(r["date"]).astimezone(dt.timezone.utc)
            except (KeyError, ValueError):
                continue
            ccy = r.get("country", "")
            out.append({"ts": ts.isoformat(timespec="minutes"), "title": r["title"], "ccy": ccy, "impact": r["impact"], "kind": "macro",
                        "assets": "all" if ccy == "USD" and r["impact"] == "High" else "", "forecast": r.get("forecast", ""), "previous": r.get("previous", ""), "src": "faireconomy"})
    return out


def deribit_expiry(now: dt.datetime | None = None) -> list[dict]:
    """Месячная экспирация опционов Deribit: последняя пятница месяца 08:00 UTC (квартальная — High, иначе Medium). Влияет на крипту."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for add in (0, 1):
        y, m0 = divmod(now.year * 12 + now.month - 1 + add, 12)
        m = m0 + 1
        d = dt.datetime(y + (m == 12), m % 12 + 1, 1, 8, tzinfo=dt.timezone.utc) - dt.timedelta(days=1)
        while d.weekday() != 4:
            d -= dt.timedelta(days=1)
        out.append({"ts": d.isoformat(timespec="minutes"), "title": "Экспирация опционов на биткоин и эфир (Deribit)", "ccy": "CRYPTO",
                    "impact": "High" if m in (3, 6, 9, 12) else "Medium", "kind": "crypto", "assets": "crypto" if m in (3, 6, 9, 12) else "", "src": "calendar"})
    return out


NY = ZoneInfo("America/New_York")


def earnings_events(symbols: list[str]) -> list[dict]:
    """Дата отчёта компании (yfinance, только дата): блок на весь день отчёта по США до 10:00 следующего дня — время выхода (до/после биржи) неизвестно."""
    import yfinance as yf
    out = []
    for s in symbols:
        if "/" in s:
            continue
        try:
            cal = cached(f"earn:{s}", 6 * 3600, lambda s=s: yf.Ticker(s).calendar)
            days = cal.get("Earnings Date") if isinstance(cal, dict) else None
        except Exception:  # noqa: BLE001
            continue
        for d in (days or [])[:1]:
            start = dt.datetime(d.year, d.month, d.day, 0, 0, tzinfo=NY).astimezone(dt.timezone.utc)
            out.append({"ts": start.isoformat(timespec="minutes"), "until": (start + dt.timedelta(hours=34)).isoformat(timespec="minutes"),
                        "title": f"Отчёт компании {s}", "ccy": "USD", "impact": "High", "kind": "earnings", "assets": s, "src": "yfinance"})
    return out
