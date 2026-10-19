"""Теневые бумажные портфели (2.1): параллельный честный счёт. Офис = реальный equity Alpaca; остальные считаются здесь по тем же ценам (снапшоты Alpaca)
и тем же издержкам: «держать S&P 500» (SPY), «держать биткоин», «только технический анализ» (long при buy ≥0,5 от аналитика market),
«только стратегии лаборатории» (long при buy от роли lab; она голосует только по approved). Правила сигнальных портфелей: до 3 позиций по 25% счёта, long-only."""
import datetime as dt
import json

from .config import COSTS, Limits, make_asset
from .risk import RiskGate

NAMES = {"office": "Весь офис", "spy": "Держать S&P 500", "btc": "Держать биткоин", "tech2": "Только технический сигнал (стопы, размер и комиссии как у офиса)", "lab": "Только стратегии лаборатории"}
SIGNAL_ROLE = {"lab": "lab"}
MAX_POS, WEIGHT, SIGNAL_MAX_AGE_H, MIN_CONF = 3, 0.25, 3, 0.5
HOLD_ASSET = {"spy": "SPY", "btc": "BTC/USD"}


def fee(asset: str) -> float:
    c = COSTS[make_asset(asset).kind]
    return c.fee + c.slippage


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _utc(s: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)   # naive = UTC


class Shadow:
    TECH = "tech2"
    COOLDOWN_H = 4.0

    def __init__(self, board, price_fn, equity_fn, now_fn=_now, lab_fn=None, start_fn=None, journal=None, candles_fn=None):
        self.b, self.price, self.equity, self.now, self.lab_fn, self.start_fn = board, price_fn, equity_fn, now_fn, lab_fn, start_fn   # start_fn: стартовый капитал офиса (журнал)
        self.j, self.candles_fn, self.gate = journal, candles_fn, RiskGate(Limits())   # journal: таблица tech_signals; candles_fn(asset)->1h DataFrame для симуляции стопов/тейков

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

    # ---- «только технический сигнал»: те же стопы, тейки, размер (RiskGate, ×0,5 — без одобрения аналитиков) и комиссии, что у офиса ----
    def _tech_exits(self, name, cash, ts):
        for a, p in list(self._positions(name).items()):
            if not p.get("stop") or not self.candles_fn:
                continue
            try:
                df = self.candles_fn(a)
            except Exception:  # noqa: BLE001 — нет свечей: позиция остаётся до следующего шага
                continue
            opened = dt.datetime.fromisoformat(p["opened"])
            if opened.tzinfo is not None:
                opened = opened.astimezone(dt.timezone.utc).replace(tzinfo=None)
            idx = df.index.tz_convert("UTC").tz_localize(None) if getattr(df.index, "tz", None) is not None else df.index
            df = df[idx > opened]
            c = COSTS[make_asset(a).kind]
            for t, bar in df.iterrows():
                lvl, why = (p["stop"], "stop") if bar["low"] <= p["stop"] else (p["take"], "take") if p["take"] and bar["high"] >= p["take"] else (None, None)   # в одной свече стоп раньше тейка (консервативно)
                if lvl:
                    px = lvl * (1 - c.slippage)
                    proceeds = p["qty"] * px * (1 - c.fee)
                    cost = p["qty"] * p["entry"]
                    self.b.x("DELETE FROM shadow_pos WHERE name=? AND asset=?", (name, a))
                    self.b.x("INSERT INTO shadow_trades(name,asset,opened,closed,entry,exit,qty,pnl,reason) VALUES(?,?,?,?,?,?,?,?,?)",
                             (name, a, p["opened"], t.isoformat(timespec="seconds"), p["entry"], px, p["qty"], proceeds - cost, why))
                    cash += proceeds
                    break
        return cash

    def _tech_step(self, name, cash, ts):
        if not self.j:
            return cash
        cash = self._tech_exits(name, cash, ts)
        last = self.j.kv_get("shadow_sig_id")
        if last is None:   # старт нового портфеля: прошлые сигналы не берём
            self.j.kv_set("shadow_sig_id", (self.j.q("SELECT COALESCE(MAX(id),0) m FROM tech_signals")[0]["m"]))
            return cash
        day = ts[:10]
        for sg in self.j.q("SELECT * FROM tech_signals WHERE id>? ORDER BY id", (last,)):
            self.j.kv_set("shadow_sig_id", sg["id"])
            pos = self._positions(name)
            a = sg["asset"]
            kind = make_asset(a).kind
            recent_stop = self.b.q("SELECT closed FROM shadow_trades WHERE name=? AND asset=? AND reason='stop' ORDER BY rowid DESC LIMIT 1", (name, a))
            if recent_stop and (_utc(sg["ts"]) - _utc(recent_stop[0]["closed"])).total_seconds() < self.COOLDOWN_H * 3600:
                continue   # то же остывание после стопа, что у офиса
            eq = cash + sum(p["qty"] * (self._px(x, p["entry"]) or p["entry"]) for x, p in pos.items())
            day0 = self.b.q("SELECT equity FROM shadow_eq WHERE name=? AND ts>=? ORDER BY ts, rowid LIMIT 1", (name, day))
            entries = (self.b.q("SELECT COUNT(*) n FROM shadow_pos WHERE name=? AND opened>=?", (name, day))[0]["n"]
                       + self.b.q("SELECT COUNT(*) n FROM shadow_trades WHERE name=? AND opened>=?", (name, day))[0]["n"])
            mult = 0.5   # технический сигнал без одобрения аналитиков = половинный объём (и против тренда тоже)
            v = self.gate.check_entry(kind=kind, costs=COSTS[kind], entry=sg["price"], stop=sg["stop"], atr=sg["atr"], equity=eq, day_start_equity=day0[0]["equity"] if day0 else eq,
                                      entries_today=entries, open_positions=len(pos), cash=cash, has_position=a in pos, size_mult=mult)
            if not v.ok:
                continue
            c = COSTS[kind]
            eff = sg["price"] * (1 + c.slippage)
            self.b.x("INSERT OR REPLACE INTO shadow_pos(name,asset,qty,entry,opened,stop,take) VALUES(?,?,?,?,?,?,?)", (name, a, v.qty, eff, sg["ts"], sg["stop"], sg["take"]))
            cash -= v.qty * eff * (1 + c.fee)
        return cash

    # ---- шаг (раз в час и после цикла) ----
    def step(self) -> dict:
        ts, eq_office = self.now().isoformat(timespec="seconds"), float(self.equity())
        out = {}
        first = not self.b.q("SELECT 1 FROM shadow_eq WHERE name='office' LIMIT 1")
        start = (self.start_fn() if self.start_fn else None) or eq_office
        if first:   # точка отсчёта офиса — стартовый капитал, а не первое попавшееся эквити (иначе убыток закрытых до наблюдения сделок не виден)
            self.b.x("INSERT INTO shadow_eq(ts,name,equity,cash) VALUES(?,?,?,?)", (ts, "office", start, start))
        self.b.x("INSERT INTO shadow_eq(ts,name,equity,cash) VALUES(?,?,?,?)", (ts, "office", eq_office, eq_office))   # cash офиса не важен: считаем по equity
        if not first:
            start = self.b.q("SELECT equity FROM shadow_eq WHERE name='office' ORDER BY ts, rowid LIMIT 1")[0]["equity"]
        for name in ("spy", "btc", self.TECH, "lab"):
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
            if name == self.TECH:
                cash = self._tech_step(name, cash, ts)
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
            q = "SELECT ts, equity FROM shadow_eq WHERE name=?" + (" AND ts>=?" if since else "") + " ORDER BY ts, rowid"
            ser = self.b.q(q, (name, since) if since else (name,))
            if len(ser) < 1:
                continue
            eqs = [r["equity"] for r in ser]
            peak, dd = eqs[0], 0.0
            for e in eqs:
                peak = max(peak, e)
                dd = min(dd, e / peak - 1)
            if name == self.TECH:
                from .views import local
                title = f"{title}, с {local(ser[0]['ts']):%d.%m}"
            rows.append({"name": name, "title": title, "start": eqs[0], "last": eqs[-1], "ret": eqs[-1] / eqs[0] - 1, "max_dd": dd, "since": ser[0]["ts"]})
        extra = self._office_since_row(rows) if not since else None
        if extra:
            i = next((k for k, r in enumerate(rows) if r["name"] == "office"), -1)
            rows.insert(i + 1, extra)
        return rows

    def _office_since_row(self, rows: list[dict]) -> dict | None:
        """«Весь офис с <дата старта теневого технического сигнала>» — офис, пересчитанный от той же точки, что и tech2: честное сравнение (общий «Весь офис» идёт с 12.10)."""
        t = next((r for r in rows if r["name"] == self.TECH), None)
        if not t:
            return None
        ser = self.b.q("SELECT ts, equity FROM shadow_eq WHERE name='office' AND ts>=? ORDER BY ts, rowid", (t["since"],))
        if len(ser) < 1:
            return None
        eqs = [r["equity"] for r in ser]
        peak, dd = eqs[0], 0.0
        for e in eqs:
            peak = max(peak, e)
            dd = min(dd, e / peak - 1)
        from .views import local
        return {"name": "office_since", "title": f"Весь офис с {local(t['since']):%d.%m}", "start": eqs[0], "last": eqs[-1], "ret": eqs[-1] / eqs[0] - 1, "max_dd": dd, "since": ser[0]["ts"]}


def table_text(rows: list[dict]) -> str:
    """Короткая русская таблица для Telegram."""
    from .report import num, pct
    return "\n".join(f"• {r['title']}: {pct(r['ret'], 2)}, просадка {num(abs(r['max_dd']) * 100, 1)}%" for r in rows)
