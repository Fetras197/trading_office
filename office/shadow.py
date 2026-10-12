"""Теневые бумажные портфели (2.1): параллельный честный счёт. Офис = реальный equity Alpaca; остальные считаются здесь по тем же ценам (снапшоты Alpaca)
и тем же издержкам: «держать S&P 500» (SPY), «держать биткоин», «только технический анализ» (long при buy ≥0,5 от аналитика market),
«только стратегии лаборатории» (long при buy от роли lab; она голосует только по approved). Правила сигнальных портфелей: до 3 позиций по 25% счёта, long-only."""
import datetime as dt
import json

from .config import COSTS, make_asset

NAMES = {"office": "Весь офис", "spy": "Держать S&P 500", "btc": "Держать биткоин", "tech": "Только технический анализ", "lab": "Только стратегии лаборатории"}
SIGNAL_ROLE = {"tech": "market", "lab": "lab"}
MAX_POS, WEIGHT, SIGNAL_MAX_AGE_H, MIN_CONF = 3, 0.25, 3, 0.5
HOLD_ASSET = {"spy": "SPY", "btc": "BTC/USD"}


def fee(asset: str) -> float:
    c = COSTS[make_asset(asset).kind]
    return c.fee + c.slippage


def _now():
    return dt.datetime.now(dt.timezone.utc)


class Shadow:
    def __init__(self, board, price_fn, equity_fn, now_fn=_now, lab_fn=None):
        self.b, self.price, self.equity, self.now, self.lab_fn = board, price_fn, equity_fn, now_fn, lab_fn

    # ---- состояние ----
    def _cash(self, name):
        r = self.b.q("SELECT cash FROM shadow_eq WHERE name=? ORDER BY ts DESC, rowid DESC LIMIT 1", (name,))
        return r[0]["cash"] if r else None

    def _positions(self, name):
        return {r["asset"]: r for r in self.b.q("SELECT * FROM shadow_pos WHERE name=?", (name,))}

    def _buy(self, name, asset, usd, px, cash):
        usd = min(usd, cash)
        if usd < 10:
            return cash
        qty = usd * (1 - fee(asset)) / px
        self.b.x("INSERT OR REPLACE INTO shadow_pos(name,asset,qty,entry,opened) VALUES(?,?,?,?,?)", (name, asset, qty, px, self.now().isoformat(timespec="seconds")))
        return cash - usd

    def _sell(self, name, asset, pos, px, cash):
        self.b.x("DELETE FROM shadow_pos WHERE name=? AND asset=?", (name, asset))
        return cash + pos["qty"] * px * (1 - fee(asset))

    def signals(self, name) -> list[tuple[str, float, float]]:
        """[(asset, conf, price)] — активы с актуальным buy от роли портфеля, лучшие первыми."""
        if name == "lab" and self.lab_fn:   # approved + поправка, включая стратегии на испытательном сроке (они торгуются здесь)
            return self.lab_fn()
        since = (self.now() - dt.timedelta(hours=SIGNAL_MAX_AGE_H)).isoformat(timespec="seconds")
        latest = {}
        for r in self.b.q("SELECT asset, confidence, data FROM reports WHERE role=? AND ts>=? ORDER BY id", (SIGNAL_ROLE[name], since)):
            latest[r["asset"]] = r
        out = []
        for a, r in latest.items():
            d = json.loads(r["data"] or "{}")
            if d.get("stance") == "buy" and r["confidence"] >= MIN_CONF:
                out.append((a, r["confidence"], d.get("price")))
        return sorted(out, key=lambda x: -x[1])[:MAX_POS]

    def _px(self, asset, fallback=None):
        try:
            return float(self.price(asset))
        except Exception:  # noqa: BLE001
            return float(fallback) if fallback else None

    # ---- шаг (раз в час и после цикла) ----
    def step(self) -> dict:
        ts, eq_office = self.now().isoformat(timespec="seconds"), float(self.equity())
        out = {}
        self.b.x("INSERT INTO shadow_eq(ts,name,equity,cash) VALUES(?,?,?,?)", (ts, "office", eq_office, eq_office))   # cash офиса не важен: считаем по equity
        start = self.b.q("SELECT equity FROM shadow_eq WHERE name='office' ORDER BY ts LIMIT 1")[0]["equity"]
        for name in ("spy", "btc", "tech", "lab"):
            cash = self._cash(name)
            if cash is None:                       # старт теневого портфеля: тот же капитал, что был у офиса в начале наблюдения
                cash = start
                self.b.x("INSERT INTO shadow_eq(ts,name,equity,cash) VALUES(?,?,?,?)", (ts, name, start, start))   # точка отсчёта до издержек входа
                if name in HOLD_ASSET:
                    px = self._px(HOLD_ASSET[name])
                    if px is None:
                        continue
                    cash = self._buy(name, HOLD_ASSET[name], cash, px, cash)
            pos = self._positions(name)
            if name in SIGNAL_ROLE:
                want = self.signals(name)
                for a, p in list(pos.items()):
                    if a not in [w[0] for w in want]:
                        px = self._px(a)
                        if px:
                            cash = self._sell(name, a, p, px, cash)
                            pos.pop(a)
                eq_now = cash + sum(p["qty"] * (self._px(a, p["entry"]) or p["entry"]) for a, p in pos.items())
                for a, _, fb in want:
                    if a not in pos and len(pos) < MAX_POS:
                        px = self._px(a, fb)
                        if px:
                            cash = self._buy(name, a, eq_now * WEIGHT, px, cash)
                            pos = self._positions(name)
            pos = self._positions(name)
            eq = cash + sum(p["qty"] * (self._px(a, p["entry"]) or p["entry"]) for a, p in pos.items())
            self.b.x("INSERT INTO shadow_eq(ts,name,equity,cash) VALUES(?,?,?,?)", (ts, name, eq, cash))
            out[name] = eq
        out["office"] = eq_office
        return out

    # ---- сравнение ----
    def summary(self, since: str | None = None) -> list[dict]:
        rows = []
        for name, title in NAMES.items():
            q = "SELECT ts, equity FROM shadow_eq WHERE name=?" + (" AND ts>=?" if since else "") + " ORDER BY ts"
            ser = self.b.q(q, (name, since) if since else (name,))
            if len(ser) < 1:
                continue
            eqs = [r["equity"] for r in ser]
            peak, dd = eqs[0], 0.0
            for e in eqs:
                peak = max(peak, e)
                dd = min(dd, e / peak - 1)
            rows.append({"name": name, "title": title, "start": eqs[0], "last": eqs[-1], "ret": eqs[-1] / eqs[0] - 1, "max_dd": dd, "since": ser[0]["ts"]})
        return rows


def table_text(rows: list[dict]) -> str:
    """Короткая русская таблица для Telegram."""
    from .report import num, pct
    return "\n".join(f"• {r['title']}: {pct(r['ret'], 2)}, просадка {num(abs(r['max_dd']) * 100, 1)}%" for r in rows)
