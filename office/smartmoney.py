"""«Умные деньги» (3.3): позиции крупных и прибыльных трейдеров Hyperliquid (бесплатное API) и зоны их ликвидаций — отдельный голос зала (крипта).
Таблица лидеров (≈39 МБ JSON) разбирается потоково (ijson) раз в 6 ч — память телефона ограничена; позиции топ-N адресов обновляются раз в 30 мин.
Coinversa Pulse не используется: бесплатного публичного доступа нет (проверено: DECISIONS). Голос считается кодом, без LLM."""
import heapq
import json
import time

import ijson
import requests

from .config import STATE_DIR

LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO = "https://api.hyperliquid.xyz/info"
CACHE = STATE_DIR / "data_cache"
TOP_N, MIN_ACCOUNT, MIN_MONTH_ROI, MIN_ALLTIME = 80, 250_000, 0.05, 250_000
MIN_COIN_USD, MIN_TRADERS, BIAS = 1_000_000, 3, 0.3
BUCKETS = (3, 5, 10, 20)   # зоны ликвидаций: до N% от текущей цены


def _perf(row, window):
    for w, d in row.get("windowPerformances", []):
        if w == window:
            return float(d.get("pnl", 0)), float(d.get("roi", 0))
    return 0.0, 0.0


def select_leaders(rows, n: int = TOP_N) -> list[dict]:
    """Самые прибыльные за месяц среди крупных счетов с положительной историей. rows — итератор (потоковый разбор)."""
    heap: list[tuple[float, str, float]] = []
    for r in rows:
        try:
            acc = float(r["accountValue"])
            m_pnl, m_roi = _perf(r, "month")
            a_pnl, _ = _perf(r, "allTime")
        except (KeyError, TypeError, ValueError):
            continue
        if acc >= MIN_ACCOUNT and m_pnl > 0 and m_roi >= MIN_MONTH_ROI and a_pnl >= MIN_ALLTIME:
            item = (m_pnl, r["ethAddress"], acc)
            if len(heap) < n:
                heapq.heappush(heap, item)
            else:
                heapq.heappushpop(heap, item)
    return [{"address": a, "month_pnl": p, "account": v} for p, a, v in sorted(heap, reverse=True)]


def fetch_leaders(post=None) -> list[dict]:
    r = requests.get(LEADERBOARD, stream=True, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    r.raw.decode_content = True
    try:
        return select_leaders(ijson.items(r.raw, "leaderboardRows.item"))
    finally:
        r.close()


def aggregate(states: list[dict], marks: dict[str, float]) -> dict:
    """states — ответы clearinghouseState по лидерам. -> {coin: {longs_usd, shorts_usd, net, traders, liq_long, liq_short}}."""
    coins: dict[str, dict] = {}
    for st in states:
        for ap in st.get("assetPositions", []):
            p = ap.get("position", {})
            try:
                coin, szi, val = p["coin"], float(p["szi"]), abs(float(p["positionValue"]))
            except (KeyError, TypeError, ValueError):
                continue
            c = coins.setdefault(coin, {"longs_usd": 0.0, "shorts_usd": 0.0, "traders": 0, "liq_long": {b: 0.0 for b in BUCKETS}, "liq_short": {b: 0.0 for b in BUCKETS}})
            c["traders"] += 1
            side = "longs_usd" if szi > 0 else "shorts_usd"
            c[side] += val
            liq, mark = p.get("liquidationPx"), marks.get(coin)
            if liq and mark:
                dist = abs(float(liq) / mark - 1) * 100
                z = c["liq_long" if szi > 0 else "liq_short"]
                for b in BUCKETS:
                    if dist <= b:
                        z[b] += val
                        break
    for c in coins.values():
        tot = c["longs_usd"] + c["shorts_usd"]
        c["net"] = round((c["longs_usd"] - c["shorts_usd"]) / tot, 3) if tot else 0.0
    return coins


def refresh(leaders_max_age_h: float = 6.0, post=None) -> dict:
    """Обновить снапшот: лидеры (≤ раз в 6 ч, потоково) + их позиции + марк-цены. Пишет data_cache/smartmoney.json."""
    CACHE.mkdir(parents=True, exist_ok=True)
    lf = CACHE / "hl_leaders.json"
    if lf.exists() and time.time() - lf.stat().st_mtime < leaders_max_age_h * 3600:
        leaders = json.loads(lf.read_text())
    else:
        leaders = fetch_leaders()
        lf.write_text(json.dumps(leaders))
    post = post or (lambda body: requests.post(INFO, json=body, timeout=20).json())
    meta, ctxs = post({"type": "metaAndAssetCtxs"})
    marks = {u["name"]: float(c["markPx"]) for u, c in zip(meta["universe"], ctxs)}
    states = []
    for ld in leaders:
        try:
            states.append(post({"type": "clearinghouseState", "user": ld["address"]}))
        except Exception:  # noqa: BLE001 — один адрес не должен ломать снапшот
            continue
        time.sleep(0.15)
    snap = {"ts": time.time(), "leaders": len(leaders), "read": len(states), "coins": aggregate(states, marks)}
    (CACHE / "smartmoney.json").write_text(json.dumps(snap))
    return snap


def load(max_age_h: float = 2.0) -> dict | None:
    f = CACHE / "smartmoney.json"
    if not f.exists():
        return None
    snap = json.loads(f.read_text())
    return snap if time.time() - snap["ts"] < max_age_h * 3600 else None


def _usd(x: float) -> str:
    return f"{x / 1e6:.1f} млн $".replace(".", ",") if x >= 1e6 else f"{x / 1e3:.0f} тыс. $"


def signal_for(coin: str, snap: dict | None = None) -> dict | None:
    """Голос зала: {stance, confidence, rationale} или None, если по монете у лидеров мало позиций (нет мнения)."""
    snap = snap if snap is not None else load()
    c = (snap or {}).get("coins", {}).get(coin)
    if not c or c["traders"] < MIN_TRADERS or c["longs_usd"] + c["shorts_usd"] < MIN_COIN_USD:
        return None
    tot = c["longs_usd"] + c["shorts_usd"]
    net = c["net"]
    near_long = sum(v for b, v in c["liq_long"].items() if b <= 5)
    near_short = sum(v for b, v in c["liq_short"].items() if b <= 5)
    stance = "buy" if net >= BIAS else "sell" if net <= -BIAS else "hold"
    conf = round(min(0.75, 0.5 + abs(net) / 3), 2) if stance != "hold" else 0.5
    side = f"в лонгах {c['longs_usd'] / tot:.0%}" if net >= 0 else f"в шортах {c['shorts_usd'] / tot:.0%}"
    txt = f"крупные прибыльные трейдеры Hyperliquid ({c['traders']}): {side} позиций на {_usd(tot)}"
    if near_long > 0.2 * tot:
        txt += f"; рядом с ценой (до 5%) ликвидации их лонгов на {_usd(near_long)}"
    if near_short > 0.2 * tot:
        txt += f"; рядом с ценой (до 5%) ликвидации их шортов на {_usd(near_short)}"
    return {"stance": stance, "confidence": conf, "rationale": txt, "net": net, "traders": c["traders"], "near_liq_long_usd": round(near_long), "near_liq_short_usd": round(near_short)}
