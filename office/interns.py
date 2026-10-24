"""Стажёры (v5.7): четыре виртуальных отдела на бумажном бюджете (20 000 $ каждому). Живых ордеров не отправляют, на боевые правила, отпечаток и счёт «20 сделок» не влияют:
модуль не входит в отпечаток (rules.snapshot), пишет только в свои таблицы intern_* платы board.db и читает чужие данные (дневные бары, картина дня, технические сигналы, отчёты аналитиков).
Общее со боевым офисом: размер, риск на сделку, дневной стоп, лимит входов в сутки — через тот же RiskGate с теми же Limits (число позиций — по замыслу стажёра, см. MAXPOS),
комиссии и проскальзывание из config.COSTS, стопы в ATR-кратностях как в entry (2·ATR; у «Аналитиков с вето» — полностью entry.trade_plan).
Исполнение: по цене последнего закрытого дневного бара ± проскальзывание (акции/ETF); «Аналитики с вето» — по цене технического сигнала, как теневой портфель tech2.
1) mom  «Импульс акций США»: топ-5 акций ликвидного списка по силе за 1–3 месяца; держим, пока цена выше 50-дневной средней.
2) sect «Ротация секторов»: раз в неделю 2–3 самых сильных отраслевых ETF (SPDR).
3) def  «Защитник»: при режиме «риск выключен» — золото (GLD) и/или TLT, при «нейтральном»/«риск включён» — в кэше.
4) vet  «Аналитики с вето»: копия боевых правил входа (технический сигнал + согласие аналитиков ⇒ полный объём, иначе половинный, режим рынка), но два самых точных аналитика могут запретить вход."""
import dataclasses
import datetime as dt
import json

import pandas as pd
import talib as ta

from . import entry, regime
from .board import ANALYSTS
from .config import COSTS, Limits, make_asset
from .risk import RiskGate

START = 20_000.0
NAMES = {"mom": "Импульс акций США", "sect": "Ротация секторов", "def": "Защитник", "vet": "Аналитики с вето"}
MAXPOS = {"mom": 5, "sect": 3, "def": 2, "vet": 3}
SECTORS = ["XLK", "XLE", "XLF", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
DEFENSIVE = ["GLD", "TLT"]
ETF_SKIP = set(SECTORS) | set(DEFENSIVE) | {"SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "IVV", "IAU", "SLV", "HYG", "LQD", "AGG", "BND", "IEF", "SHY", "EEM", "EFA", "SMH", "SOXX", "XBI", "ARKK",
                                              "USO", "UNG", "TQQQ", "SQQQ", "SOXL", "SOXS", "UVXY", "VXX", "SVXY", "TLH", "VNQ", "KRE", "XOP", "GDX"}
TOP_N, MOM_DAYS_1M, MOM_DAYS_3M, SMA_DAYS, ATR_DAYS = 5, 21, 63, 50, 14
SECT_MIN, SECT_MAX = 2, 3
STOP_ATR = entry.STOP_ATR                    # тот же стоп: 2 ATR (здесь — дневной ATR, горизонт недели–месяцы)
MIN_DAYS_VERDICT, MIN_TRADES_VERDICT = 28, 6
PROMOTE_MARGIN, FIRE_RET, FIRE_DD = 0.02, -0.05, -0.08
VETO_BARS_BEFORE_H, VETO_BARS_AFTER_H, SIGNAL_WAIT_MIN = 0.5, 2.0, 15

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS intern_pos(name TEXT, asset TEXT, qty REAL, entry REAL, opened TEXT, stop REAL, take REAL, why TEXT, PRIMARY KEY(name, asset))",
    "CREATE TABLE IF NOT EXISTS intern_trades(name TEXT, asset TEXT, opened TEXT, closed TEXT, entry REAL, exit REAL, qty REAL, pnl REAL, reason TEXT)",
    "CREATE TABLE IF NOT EXISTS intern_eq(ts TEXT, name TEXT, equity REAL, cash REAL)",
    "CREATE INDEX IF NOT EXISTS ix_ieq ON intern_eq(name, ts)",
]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc(s: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def kind_of(asset: str) -> str:
    return make_asset(asset).kind


def score_momentum(close: pd.Series) -> float | None:
    """Сила = среднее доходности за 1 и за 3 месяца. Нужно ≥ SMA_DAYS+1 баров и 63+ баров истории."""
    if len(close) < max(MOM_DAYS_3M + 1, SMA_DAYS + 1):
        return None
    c = close.astype(float)
    return 0.5 * (c.iloc[-1] / c.iloc[-1 - MOM_DAYS_1M] - 1) + 0.5 * (c.iloc[-1] / c.iloc[-1 - MOM_DAYS_3M] - 1)


def above_sma(close: pd.Series) -> bool:
    c = close.astype(float)
    return len(c) >= SMA_DAYS and float(c.iloc[-1]) > float(c.tail(SMA_DAYS).mean())


def daily_atr(df: pd.DataFrame) -> float | None:
    if len(df) < ATR_DAYS + 2:
        return None
    v = ta.ATR(df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float), ATR_DAYS)[-1]
    return float(v) if v == v and v > 0 else None


def pick_top(bars: dict[str, pd.DataFrame], n: int = TOP_N, skip=ETF_SKIP) -> list[tuple[str, float]]:
    """[(тикер, сила)] — только акции выше 50-дневной средней, сильнейшие первыми."""
    out = []
    for s, df in bars.items():
        if s in skip or df is None or df.empty:
            continue
        sc = score_momentum(df["close"])
        if sc is not None and sc > 0 and above_sma(df["close"]):
            out.append((s, sc))
    return sorted(out, key=lambda x: -x[1])[:n]


def pick_sectors(bars: dict[str, pd.DataFrame]) -> list[tuple[str, float]]:
    """2–3 самых сильных отраслевых ETF; ETF с отрицательной силой не берём (если таких меньше двух — держим меньше)."""
    sc = []
    for s in SECTORS:
        df = bars.get(s)
        v = score_momentum(df["close"]) if df is not None and not df.empty else None
        if v is not None:
            sc.append((s, v))
    sc.sort(key=lambda x: -x[1])
    return [x for x in sc[:SECT_MAX] if x[1] > 0]


class Interns:
    def __init__(self, board, journal, universe_fn, bars_fn, candles_fn, price_fn, regime_fn=None, now_fn=_utcnow):
        """universe_fn() -> [тикеры акций]; bars_fn(symbols, days) -> {sym: дневные бары}; candles_fn(asset) -> 1h бары (для стопов «Аналитиков с вето»); price_fn(asset) -> цена."""
        self.b, self.j, self.universe_fn, self.bars_fn, self.candles_fn, self.price = board, journal, universe_fn, bars_fn, candles_fn, price_fn
        self.regime_fn, self.now = regime_fn or (lambda: self._regime_from_board()), now_fn
        for s in SCHEMA:
            self.b.x(s)

    # ---- состояние ----
    def gate(self, name: str) -> RiskGate:
        return RiskGate(dataclasses.replace(Limits(), max_positions=MAXPOS[name]))

    def cash(self, name: str) -> float:
        r = self.b.q("SELECT cash FROM intern_eq WHERE name=? ORDER BY ts DESC, rowid DESC LIMIT 1", (name,))
        return r[0]["cash"] if r else START

    def positions(self, name: str) -> dict:
        return {r["asset"]: r for r in self.b.q("SELECT * FROM intern_pos WHERE name=?", (name,))}

    def _px(self, asset: str, fallback: float) -> float:
        try:
            return float(self.price(asset))
        except Exception:  # noqa: BLE001
            return fallback

    def equity(self, name: str, cash: float | None = None, marks: dict | None = None) -> float:
        cash = self.cash(name) if cash is None else cash
        marks = marks or {}
        return cash + sum(p["qty"] * (marks.get(a) or self._px(a, p["entry"])) for a, p in self.positions(name).items())

    def snapshot(self, name: str, cash: float, ts: str, marks: dict | None = None) -> float:
        eq = self.equity(name, cash, marks)
        self.b.x("INSERT INTO intern_eq(ts,name,equity,cash) VALUES(?,?,?,?)", (ts, name, eq, cash))
        return eq

    def _day0(self, name: str, ts: str) -> float:
        """Эквити на начало суток (UTC): первая запись за сегодня, иначе последняя до сегодняшнего дня, иначе стартовый капитал."""
        r = self.b.q("SELECT equity FROM intern_eq WHERE name=? AND ts>=? ORDER BY ts, rowid LIMIT 1", (name, ts[:10]))
        if r:
            return r[0]["equity"]
        r = self.b.q("SELECT equity FROM intern_eq WHERE name=? AND ts<? ORDER BY ts DESC, rowid DESC LIMIT 1", (name, ts[:10]))
        return r[0]["equity"] if r else START

    def _entries_today(self, name: str, ts: str) -> int:
        day = ts[:10]
        return (self.b.q("SELECT COUNT(*) n FROM intern_pos WHERE name=? AND opened>=?", (name, day))[0]["n"]
                + self.b.q("SELECT COUNT(*) n FROM intern_trades WHERE name=? AND opened>=?", (name, day))[0]["n"])

    # ---- сделки ----
    def buy(self, name: str, asset: str, price: float, stop: float, take: float | None, atr: float | None, why: str, ts: str, cash: float, mult: float = 1.0) -> float:
        kind = kind_of(asset)
        c = COSTS[kind]
        eq = self.equity(name, cash)
        v = self.gate(name).check_entry(kind=kind, costs=c, entry=price, stop=stop, atr=atr, equity=eq, day_start_equity=self._day0(name, ts), entries_today=self._entries_today(name, ts),
                                        open_positions=len(self.positions(name)), cash=cash, has_position=asset in self.positions(name), size_mult=mult)
        if not v.ok:
            return cash
        eff = price * (1 + c.slippage)
        self.b.x("INSERT OR REPLACE INTO intern_pos(name,asset,qty,entry,opened,stop,take,why) VALUES(?,?,?,?,?,?,?,?)", (name, asset, v.qty, eff, ts, stop, take, why))
        return cash - v.qty * eff * (1 + c.fee)

    def sell(self, name: str, asset: str, px: float, reason: str, ts: str, cash: float, slip: bool = True) -> float:
        p = self.positions(name)[asset]
        c = COSTS[kind_of(asset)]
        out = px * (1 - c.slippage) if slip else px
        proceeds = p["qty"] * out * (1 - c.fee)
        pnl = proceeds - p["qty"] * p["entry"] * (1 + c.fee)
        self.b.x("DELETE FROM intern_pos WHERE name=? AND asset=?", (name, asset))
        self.b.x("INSERT INTO intern_trades(name,asset,opened,closed,entry,exit,qty,pnl,reason) VALUES(?,?,?,?,?,?,?,?,?)", (name, asset, p["opened"], ts, p["entry"], out, p["qty"], pnl, reason))
        return cash + proceeds

    def _stop_hits(self, name: str, bars: dict[str, pd.DataFrame], ts: str, cash: float) -> float:
        """Защитные стоп/тейк по дневным барам после открытия (в одной свече стоп раньше тейка — консервативно)."""
        for a, p in list(self.positions(name).items()):
            df = bars.get(a)
            if df is None or df.empty:
                continue
            opened = _utc(p["opened"]).replace(tzinfo=None)
            idx = df.index.tz_convert("UTC").tz_localize(None) if getattr(df.index, "tz", None) is not None else df.index
            for t, bar in df[idx.normalize() > pd.Timestamp(opened).normalize()].iterrows():
                if p["stop"] and bar["low"] <= p["stop"]:
                    cash = self.sell(name, a, p["stop"], "stop", ts, cash)
                    break
                if p["take"] and bar["high"] >= p["take"]:
                    cash = self.sell(name, a, p["take"], "take", ts, cash)
                    break
        return cash

    # ---- режим рынка (картина дня) ----
    def _regime_from_board(self) -> str:
        rep = self.b.latest("picture", max_age_h=36)
        return (rep["data"].get("regime") if rep else None) or "neutral"

    # ---- дневные стратегии ----
    def _fetch(self, symbols: list[str], days: int = 200) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        syms = sorted(set(symbols))
        for i in range(0, len(syms), 150):
            try:
                out.update(self.bars_fn(syms[i:i + 150], days))
            except Exception:  # noqa: BLE001 — сбой источника: стратегия пропускает день
                continue
        return out

    def _buy_asset(self, name, asset, df, why, ts, cash, take=False):
        atr = daily_atr(df)
        if not atr:
            return cash
        px = float(df["close"].iloc[-1])
        return self.buy(name, asset, px, px - STOP_ATR * atr, px + entry.TAKE_ATR * atr if take else None, atr, why, ts, cash)

    def run_mom(self, bars: dict, ts: str) -> None:
        name, cash = "mom", self.cash("mom")
        cash = self._stop_hits(name, bars, ts, cash)
        for a, p in list(self.positions(name).items()):               # держим, пока выше 50-дневной
            df = bars.get(a)
            if df is not None and not df.empty and not above_sma(df["close"]):
                cash = self.sell(name, a, float(df["close"].iloc[-1]), "ниже 50-дневной средней", ts, cash)
        top = pick_top(bars)
        for a, sc in top:
            if a not in self.positions(name) and len(self.positions(name)) < MAXPOS[name]:
                cash = self._buy_asset(name, a, bars[a], f"сила {sc * 100:+.0f}% за 1–3 месяца", ts, cash)
        self.snapshot(name, cash, ts, {a: float(d["close"].iloc[-1]) for a, d in bars.items() if a in self.positions(name)})

    def run_sect(self, bars: dict, ts: str, now: dt.datetime) -> None:
        name, cash = "sect", self.cash("sect")
        cash = self._stop_hits(name, bars, ts, cash)
        iso = now.isocalendar()
        week = f"{iso[0]}-{iso[1]}"
        if self.j.kv_get("interns_sect_week") != week:               # раз в неделю: ротация
            want = pick_sectors(bars)
            if want or bars:
                keep = {a for a, _ in want}
                for a in list(self.positions(name)):
                    if a not in keep and a in bars:
                        cash = self.sell(name, a, float(bars[a]["close"].iloc[-1]), "ротация: вышел из сильнейших", ts, cash)
                for a, sc in want:
                    if a not in self.positions(name):
                        cash = self._buy_asset(name, a, bars[a], f"в сильнейших отраслях: {sc * 100:+.0f}% за 1–3 месяца", ts, cash)
                self.j.kv_set("interns_sect_week", week)
        self.snapshot(name, cash, ts, {a: float(d["close"].iloc[-1]) for a, d in bars.items() if a in self.positions(name)})

    def run_def(self, bars: dict, ts: str) -> None:
        name, cash = "def", self.cash("def")
        cash = self._stop_hits(name, bars, ts, cash)
        reg = self.regime_fn()
        if reg == "risk_off":
            for a in DEFENSIVE:
                if a in bars and a not in self.positions(name) and len(self.positions(name)) < MAXPOS[name]:
                    cash = self._buy_asset(name, a, bars[a], "режим «риск выключен» — защитный актив", ts, cash)
        else:
            for a in list(self.positions(name)):
                if a in bars:
                    cash = self.sell(name, a, float(bars[a]["close"].iloc[-1]), f"режим «{'риск включён' if reg == 'risk_on' else 'нейтральный'}» — уходим в кэш", ts, cash)
        self.snapshot(name, cash, ts, {a: float(d["close"].iloc[-1]) for a, d in bars.items() if a in self.positions(name)})

    # ---- «Аналитики с вето» ----
    def vetoers(self) -> list[str]:
        """Два самых точных аналитика (по оценённым мнениям, не меньше weights.MIN_N); если оценок пока нет — последний известный список из журнала."""
        from . import weights
        st = weights.stats(self.b)
        ranked = sorted(((v["acc"], v["n"], r) for r, v in st.items() if r in ANALYSTS and v["n"] >= weights.MIN_N), reverse=True)
        top = [r for _, _, r in ranked[:2]]
        if top:
            self.j.kv_set("interns_vetoers", top)
            return top
        return self.j.kv_get("interns_vetoers") or []

    def _reports_near(self, asset: str, ts: str) -> list[dict]:
        t = _utc(ts)
        lo, hi = (t - dt.timedelta(hours=VETO_BARS_BEFORE_H)).isoformat(timespec="seconds"), (t + dt.timedelta(hours=VETO_BARS_AFTER_H)).isoformat(timespec="seconds")
        latest = {}
        for r in self.b.q("SELECT role, data FROM reports WHERE asset=? AND ts>=? AND ts<=? ORDER BY id", (asset, lo, hi)):
            if r["role"] in ANALYSTS:
                latest[r["role"]] = json.loads(r["data"] or "{}")
        return [{"role": k, **v} for k, v in latest.items()]

    def _vet_exits(self, cash: float) -> float:
        name = "vet"
        for a, p in list(self.positions(name).items()):
            try:
                df = self.candles_fn(a)
            except Exception:  # noqa: BLE001
                continue
            if df is None or df.empty:
                continue
            opened = _utc(p["opened"]).replace(tzinfo=None)
            idx = df.index.tz_convert("UTC").tz_localize(None) if getattr(df.index, "tz", None) is not None else df.index
            for t, bar in df[idx > opened].iterrows():
                lvl, why = (p["stop"], "stop") if bar["low"] <= p["stop"] else (p["take"], "take") if p["take"] and bar["high"] >= p["take"] else (None, None)
                if lvl:
                    cash = self.sell(name, a, lvl, why, t.isoformat(timespec="seconds"), cash)
                    break
        return cash

    def run_vet(self, ts: str) -> dict:
        name = "vet"
        cash = self._vet_exits(self.cash(name))
        last = self.j.kv_get("interns_sig_id")
        out = {"entered": 0, "vetoed": 0, "skipped": 0}
        if last is None:                                              # старт: прошлые сигналы не берём
            self.j.kv_set("interns_sig_id", self.j.q("SELECT COALESCE(MAX(id),0) m FROM tech_signals")[0]["m"])
            self.snapshot(name, cash, ts)
            return out
        vet = self.vetoers()
        ready = (_utc(ts) - dt.timedelta(minutes=SIGNAL_WAIT_MIN)).isoformat(timespec="seconds")
        for sg in self.j.q("SELECT * FROM tech_signals WHERE id>? AND ts<=? ORDER BY id", (last, ready)):
            self.j.kv_set("interns_sig_id", sg["id"])
            a, kind = sg["asset"], kind_of(sg["asset"])
            pos = self.positions(name)
            if a in pos:
                continue
            stopped = self.b.q("SELECT closed FROM intern_trades WHERE name=? AND asset=? AND reason='stop' ORDER BY rowid DESC LIMIT 1", (name, a))
            if stopped and (_utc(sg["ts"]) - _utc(stopped[0]["closed"])).total_seconds() < entry.COOLDOWN_H * 3600:
                out["skipped"] += 1
                continue
            reps = self._reports_near(a, sg["ts"])
            if any(r["role"] in vet and r.get("stance") == "sell" for r in reps):     # вето: один из двух самых точных аналитиков против
                out["vetoed"] += 1
                continue
            reg = (regime.current(self.b, kind, max_age_h=36) or {}).get("regime")
            if reg == "panic":
                out["skipped"] += 1
                continue
            plan = entry.trade_plan(sg["price"], sg["atr"], sg["kind"], kind, COSTS[kind])
            if not plan.ok:
                out["skipped"] += 1
                continue
            mult = 1.0 if entry.majority(entry.count_votes(reps)) else entry.HALF          # как у офиса: без согласия аналитиков — половинный объём
            mult *= regime.SIZE_MULT.get(reg or "trend", 1.0)
            before = len(self.positions(name))
            cash = self.buy(name, a, sg["price"], plan.stop, plan.take, sg["atr"], f"сигнал «{entry.TECH_RU.get(sg['kind'], sg['kind'])}»", sg["ts"], cash, mult)
            out["entered"] += len(self.positions(name)) - before
        self.snapshot(name, cash, ts)
        return out

    # ---- шаг (раз в час: сам решает, что делать) ----
    def step(self) -> dict:
        now = self.now()
        ts = now.isoformat(timespec="seconds")
        out = {}
        spy = self._fetch(["SPY"], 30).get("SPY")
        bar_day = str(spy.index[-1].date()) if spy is not None and not spy.empty else None
        if bar_day and self.j.kv_get("interns_bar_day") != bar_day:     # новый дневной бар: считаем дневных стажёров
            held = {a for n in ("mom", "sect", "def") for a in self.positions(n)}
            uni = [s for s in self.universe_fn() if s not in ETF_SKIP]
            bars = self._fetch(uni + SECTORS + DEFENSIVE + ["SPY"] + sorted(held))
            self.run_mom(bars, ts)
            self.run_sect(bars, ts, now)
            self.run_def(bars, ts)
            self.j.kv_set("interns_bar_day", bar_day)
            out["daily"] = bar_day
        else:
            for n in ("mom", "sect", "def"):                            # нового дневного бара нет (выходные, биржа закрыта): переоцениваем позиции по текущим ценам — иначе в отчёте за день дневных стажёров нет
                self.snapshot(n, self.cash(n), ts)
        out["vet"] = self.run_vet(ts)
        return out

    # ---- отчёты ----
    def rows(self, since: str | None = None) -> list[dict]:
        out = []
        for name, title in NAMES.items():
            q = "SELECT ts, equity FROM intern_eq WHERE name=?" + (" AND ts>=?" if since else "") + " ORDER BY ts, rowid"
            ser = self.b.q(q, (name, since) if since else (name,))
            if not ser and since:        # за период снимков нет (нет нового дневного бара): показываем последнее известное значение, а не пропускаем стажёра
                ser = self.b.q("SELECT ts, equity FROM intern_eq WHERE name=? ORDER BY ts DESC, rowid DESC LIMIT 1", (name,))
            if not ser:
                continue
            eqs = [r["equity"] for r in ser]
            first = START if not since else eqs[0]
            peak, dd = first, 0.0
            for e in eqs:
                peak = max(peak, e)
                dd = min(dd, e / peak - 1)
            tq = "SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl, COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) wins FROM intern_trades WHERE name=?" + (" AND closed>=?" if since else "")
            t = self.b.q(tq, (name, since) if since else (name,))[0]
            out.append({"name": name, "title": title, "start": first, "last": eqs[-1], "ret": eqs[-1] / first - 1, "max_dd": dd, "trades": t["n"], "wins": t["wins"], "pnl": t["pnl"],
                        "open": len(self.positions(name)), "since": ser[0]["ts"]})
        return out

    def lines(self, since: str | None = None, title: str = "🎓 Стажёры (бумажные 20 000 $ каждому; на боевые правила и счёт сделок не влияют):") -> str:
        from .report import num, pct, usd
        rows = self.rows(since)
        if not rows:
            return title + "\nСтажёры ещё не начали считаться."
        out = [title]
        for r in rows:
            total = r["last"] / START - 1
            res = f"{pct(r['ret'], 2)} за период, с начала {pct(total, 2)}" if since else pct(r["ret"], 2)
            out.append(f"• {r['title']}: {res} ({usd(r['last'])}), просадка {num(abs(r['max_dd']) * 100, 1)}%, открыто позиций {r['open']}, закрыто сделок {r['trades']}")
        return "\n".join(out)

    # ---- воскресный совет: сравнение и предложения ----
    def verdicts(self, j, desk_rows: list[dict], now: dt.datetime) -> list[str]:
        """Сравнение с боевыми отделами и предложения владельцу. Решение принимает владелец: переход в боевые меняет правила и начинает счёт сделок заново."""
        from .report import num, pct
        rows = {r["name"]: r for r in self.rows()}
        if not rows:
            return ["Стажёры ещё не накопили данных."]
        spy = self.b.q("SELECT equity FROM shadow_eq WHERE name='spy' ORDER BY ts, rowid")
        first = min((r["since"] for r in rows.values()), default=None)
        spy_ret = None
        if spy and first:
            ser = self.b.q("SELECT equity FROM shadow_eq WHERE name='spy' AND ts>=? ORDER BY ts, rowid", (first,))
            spy_ret = ser[-1]["equity"] / ser[0]["equity"] - 1 if len(ser) >= 2 else None
        desk_best = None
        for d in desk_rows:
            if d.get("budget"):
                r = d["pnl"] / d["budget"]
                desk_best = r if desk_best is None else max(desk_best, r)
        out = []
        if desk_rows:
            out.append("Боевые отделы (за неделю, на бюджет): " + "; ".join(f"«{d['name']}» {pct(d['pnl'] / d['budget'], 2)}" for d in desk_rows if d.get("budget")) + ".")
        for name, r in rows.items():
            days = (now - _utc(r["since"])).days
            head = f"«{r['title']}»: {pct(r['ret'], 2)} за {days} дн., просадка {num(abs(r['max_dd']) * 100, 1)}%, сделок {r['trades']}"
            if spy_ret is not None:
                head += f", S&P 500 за тот же срок {pct(spy_ret, 2)}"
            enough = days >= MIN_DAYS_VERDICT and r["trades"] >= MIN_TRADES_VERDICT
            if not enough:
                out.append(head + f" — наблюдаем: для решения нужно не меньше {MIN_DAYS_VERDICT} дней и {MIN_TRADES_VERDICT} закрытых сделок.")
            elif r["ret"] < FIRE_RET or (spy_ret is not None and r["ret"] < spy_ret - 0.05 and r["max_dd"] < FIRE_DD):
                out.append(head + " — ПРЕДЛАГАЮ УВОЛИТЬ: слабее рынка и теряет деньги.")
            elif r["ret"] > max(spy_ret or 0.0, 0.0) + PROMOTE_MARGIN and r["max_dd"] > -0.10:
                cmp_ = "" if desk_best is None else (", сильнее лучшего боевого отдела" if r["ret"] > desk_best else ", но слабее лучшего боевого отдела")
                out.append(head + f" — ПРЕДЛАГАЮ ПЕРЕВЕСТИ В БОЕВЫЕ{cmp_}. Перевод меняет боевые правила (отпечаток) и начинает счёт «20 сделок» заново — решаете вы.")
            else:
                out.append(head + " — наблюдаем: результат в пределах нормы.")
        return out

    def council_text(self, j, desk_rows: list[dict], now: dt.datetime) -> str:
        return "🎓 Стажёры и совет\n" + "\n".join(f"• {s}" for s in self.verdicts(j, desk_rows, now))
