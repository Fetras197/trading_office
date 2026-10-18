"""Бесплатные источники данных исследовательского отдела (без ключей). Всё best-effort: при сбое возвращается {"error": ...} или [],
исследователь тогда пишет abstain / работает по остальным данным. Источники проверены 2026-10-12 из Termux/proot:
Yahoo chart (VIX, DXY, доходности, индексы, сектора), NY Fed (EFFR), BLS v1 (CPI, безработица), Faireconomy (календарь), Polymarket gamma,
OKX public (funding, OI, ликвидации — Binance futures/Bybit из региона недоступны), alternative.me (индекс страха и жадности)."""
import datetime as dt
import io
import json
import re
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


FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_MAP = {"^VIX": "VIXCLS", "DX-Y.NYB": "DTWEXBGS", "^TNX": "DGS10", "^FVX": "DGS5", "^IRX": "DGS3MO", "^GSPC": "SP500", "CL=F": "DCOILWTICO"}  # DXY → широкий индекс доллара ФРС (другой масштаб, динамика та же)
_FRED_START = {"5d": 14, "1mo": 45, "3mo": 120, "6mo": 220, "1y": 400, "2y": 760}


def fred_series(sid: str, days: int = 220) -> pd.Series:
    """Запасной источник макро: FRED CSV без ключа (данные с запозданием в 1 рабочий день)."""
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    for i in range(3):   # FRED режет «браузерный» User-Agent (зависание/HTTP2-сброс): ходим с дефолтным python-requests и повторяем
        try:
            r = requests.get(FRED, params={"id": sid, "cosd": start}, timeout=25)
            r.raise_for_status()
            break
        except requests.RequestException:
            if i == 2:
                raise
            time.sleep(2)
    df = pd.read_csv(io.StringIO(r.text))
    s = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    s.index = pd.to_datetime(df.iloc[:, 0])
    s = s.dropna()
    if s.empty:
        raise ValueError(f"FRED {sid}: пусто")
    return s


def yahoo_close_src(sym: str, rng: str = "6mo") -> tuple[pd.Series, str]:
    """Дневные закрытия и имя источника: Yahoo → бары Alpaca (ETF/акции) → FRED (индексы, доходности, нефть)."""
    try:
        r = data.stock_candles(sym, "1d", rng)
        return r["close"].dropna(), ("Yahoo Finance" if data.LAST_STOCK_SRC["src"] == "yahoo" else "Alpaca")
    except Exception:  # noqa: BLE001
        if sym not in FRED_MAP:
            raise
        return fred_series(FRED_MAP[sym], _FRED_START.get(rng, 220)), "FRED"


def yahoo_close(sym: str, rng: str = "6mo") -> pd.Series:
    return yahoo_close_src(sym, rng)[0]


def _chg(s: pd.Series, n: int) -> float | None:
    return round(float(s.iloc[-1] / s.iloc[-1 - n] - 1), 4) if len(s) > n else None


def market_levels() -> dict:
    """Уровни и изменения за 1/5/20 дней: VIX, DXY, доходности США (в %, ^TNX уже в %), S&P 500, золото, нефть."""
    out = {}
    for k, sym in YAHOO_MACRO.items():
        try:
            s, src = yahoo_close_src(sym)
            row = {"last": round(float(s.iloc[-1]), 3), "chg_1d": _chg(s, 1), "chg_5d": _chg(s, 5), "chg_20d": _chg(s, 20), "src": src, "asof": str(s.index[-1])[:10]}
            if k == "SPX" and len(s) >= 50:
                row["above_sma50"] = bool(s.iloc[-1] > s.tail(50).mean())
            out[k] = row
        except Exception as e:  # noqa: BLE001
            out[k] = {"error": type(e).__name__}
    if all("last" in out.get(k, {}) for k in ("US10Y", "US3M")):
        out["curve_10y_3m"] = round(out["US10Y"]["last"] - out["US3M"]["last"], 3)
        out["curve_src"] = "расчёт: US10Y минус US3M"
    return out


def effr() -> dict:
    j = _get_json("https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json")["refRates"][0]
    return {"effr": j["percentRate"], "target_from": j["targetRateFrom"], "target_to": j["targetRateTo"], "date": j["effectiveDate"], "src": "NY Fed"}


def bls_macro() -> dict:
    """CPI (all items, NSA) y/y и безработица. BLS v1 без ключа (25 запросов/сутки на IP, из облака часто режется) → запасной FRED (CPIAUCNS, UNRATE)."""
    try:
        return _bls()
    except Exception:  # noqa: BLE001
        cpi, un = fred_series("CPIAUCNS", 500), fred_series("UNRATE", 120)
        last = cpi.index[-1]
        prev = cpi[cpi.index.to_period("M") == last.to_period("M") - 12]
        return {"cpi_period": f"{last:%Y-%m}", "cpi_yoy": round(float(cpi.iloc[-1] / prev.iloc[-1] - 1), 4) if len(prev) else None,
                "unemployment_period": f"{un.index[-1]:%Y-%m}", "unemployment": float(un.iloc[-1]), "src": "FRED"}


def _bls() -> dict:
    r = requests.post("https://api.bls.gov/publicAPI/v1/timeseries/data/", json={"seriesid": ["CUUR0000SA0", "LNS14000000"]}, headers=UA, timeout=30).json()
    ser = {s["seriesID"]: [d for d in s["data"] if d["value"].replace(".", "", 1).isdigit()] for s in r["Results"]["series"]}  # у BLS бывают пропуски ("-")
    cpi, un = ser["CUUR0000SA0"], ser["LNS14000000"]
    vals = {(d["year"], d["period"]): float(d["value"]) for d in cpi}
    last = cpi[0]
    prev_year = vals.get((str(int(last["year"]) - 1), last["period"]))
    return {"cpi_period": f"{last['year']}-{last['period'][1:]}", "cpi_yoy": round(float(last["value"]) / prev_year - 1, 4) if prev_year else None,
            "unemployment_period": f"{un[0]['year']}-{un[0]['period'][1:]}", "unemployment": float(un[0]["value"]), "src": "BLS"}


def fear_greed() -> dict:
    j = _get_json("https://api.alternative.me/fng/?limit=2")["data"]
    return {"value": int(j[0]["value"]), "label": j[0]["value_classification"], "prev": int(j[1]["value"]) if len(j) > 1 else None, "src": "alternative.me"}


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


# ---------------- источники каждой цифры (для «Картины дня») ----------------
LEVEL_RU = {"VIX": "VIX (индекс страха)", "DXY": "индекс доллара", "US10Y": "доходность 10-летних облигаций США, %", "US5Y": "доходность 5-летних облигаций США, %",
            "US3M": "доходность 3-месячных векселей США, %", "SPX": "S&P 500", "GOLD": "золото", "OIL": "нефть WTI"}


def figures(d: dict) -> list[dict]:
    """Плоский список макро-цифр с источником и датой: {label, value, unit, src, asof, nums}. Единственный разрешённый источник чисел для текстов «Картины дня».
    nums — все числа, которые можно цитировать (уровень, изменения в %, вероятности)."""
    out = []
    lv = d.get("levels") or {}
    for k, ru in LEVEL_RU.items():
        r = lv.get(k) or {}
        if "last" in r:
            nums = [r["last"]] + [100 * r[c] for c in ("chg_1d", "chg_5d", "chg_20d") if r.get(c) is not None]
            out.append({"label": ru, "value": r["last"], "src": r.get("src", "?"), "asof": r.get("asof", ""), "nums": nums})
    if "curve_10y_3m" in lv:
        out.append({"label": "разница доходностей 10 лет и 3 месяца, п.п.", "value": lv["curve_10y_3m"], "src": lv.get("curve_src", "расчёт"), "asof": "", "nums": [lv["curve_10y_3m"]]})
    f = d.get("fed") or {}
    if "effr" in f:
        out.append({"label": "ставка ФРС (EFFR), %", "value": f["effr"], "src": f.get("src", "NY Fed"), "asof": f.get("date", ""), "nums": [f["effr"], f["target_from"], f["target_to"]]})
    c = d.get("inflation_labor") or {}
    if c.get("cpi_yoy") is not None:
        out.append({"label": "инфляция CPI за год, %", "value": round(100 * c["cpi_yoy"], 2), "src": c.get("src", "BLS"), "asof": c.get("cpi_period", ""), "nums": [100 * c["cpi_yoy"]]})
    if c.get("unemployment") is not None:
        out.append({"label": "безработица, %", "value": c["unemployment"], "src": c.get("src", "BLS"), "asof": c.get("unemployment_period", ""), "nums": [c["unemployment"]]})
    g = d.get("fear_greed") or {}
    if "value" in g:
        out.append({"label": "индекс страха и жадности (крипта)", "value": g["value"], "src": g.get("src", "alternative.me"), "asof": "", "nums": [g["value"], g.get("prev") or 0]})
    for e in (d.get("prediction_markets") or [])[:4]:
        for m in e.get("markets", [])[:3]:
            out.append({"label": f"Polymarket: {m['question'][:60]}", "value": round(100 * m["yes"], 1), "src": "Polymarket", "asof": e.get("ends", ""), "nums": [100 * m["yes"]]})
    return out


def figures_text(figs: list[dict], limit: int = 9) -> str:
    """Строка «Данные и источники» — собрана кодом, не LLM."""
    parts = [f"{f['label']}: {f['value']:g} ({f['src']}{', ' + f['asof'] if f['asof'] else ''})" for f in figs[:limit]]
    return "; ".join(parts)


_NUM = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)(\s*%)?")


def unsourced_numbers(text: str, figs: list[dict], extra: str = "") -> list[str]:
    """Числа в тексте LLM (десятичные или с %), которых нет среди цифр с источником и во входных данных (допуск 2% или 0,06): кандидаты в выдумку."""
    allowed = [abs(x) for f in figs for x in f["nums"]]
    allowed += [float(t.replace(",", ".")) for t in re.findall(r"\d+(?:[.,]\d+)?", extra)]   # числа из остальных входных данных (календарь, счёт) — тоже не выдумка
    bad = []
    for m in _NUM.finditer(text):
        raw = m.group(1)
        if not ("," in raw or "." in raw or m.group(2)):
            continue
        x = float(raw.replace(",", "."))
        if not any(abs(x - a) <= max(0.06, 0.02 * a) for a in allowed):
            bad.append(raw + (m.group(2) or "").strip())
    return bad
