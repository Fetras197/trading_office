"""Сопровождение открытых позиций (3.2), каждые 5 минут. Только long. Стоп ТОЛЬКО поднимается (никогда не опускается), риск на сделку не растёт.
· безубыток: цена ≥ вход + 1R  →  стоп на вход + издержки;
· подтяжка: цена ≥ вход + 1,5R →  стоп = цена − 2·ATR (если выше текущего хотя бы на 0,25 ATR и не ближе 0,3% к цене);
· частичная фиксация: половина позиции при +3% (или на уровне partial_take, который задал трейдер), остаток — стоп в безубыток;
· выход по времени: позиции ≥ 48 ч, и за последние 48 ч диапазон цены < 1,5% — рынок стоит, капитал освобождается.
R = вход − начальный стоп (запоминается при первом обнаружении позиции)."""
import datetime as dt

from . import config, data, indicators, report
from .alpaca import AlpacaError
from .config import COSTS

BE_R, TRAIL_R, TRAIL_ATR, MIN_STEP_ATR, MIN_GAP = 1.0, 1.5, 2.0, 0.25, 0.003
PARTIAL_PCT, PARTIAL_FRAC = 0.03, 0.5
STALE_H, STALE_RANGE = 48, 0.015


def decide(p: dict, price: float, atr: float, init_stop: float, partial_done: bool, partial_at: float | None, age_h: float, range48: float | None, kind: str) -> dict:
    """Чистая функция решения. Возвращает {'close': причина|None, 'partial': bool, 'stop': новый стоп|None, 'why': [...]}."""
    avg, stop = p["avg"], p["stop"]
    R = max(avg - init_stop, avg * 0.002)
    fee = 2 * (COSTS[kind].fee + COSTS[kind].slippage)
    out = {"close": None, "partial": False, "stop": None, "why": []}
    if age_h >= STALE_H and range48 is not None and range48 < STALE_RANGE and price < avg + BE_R * R:
        out["close"] = "time"
        out["why"].append("цена стоит 48 часов")
        return out
    new_stop = stop
    be_ok = price >= avg + BE_R * R or partial_done
    if be_ok:
        new_stop = max(new_stop, avg * (1 + fee))
        out["why"].append("безубыток")
    if price >= avg + TRAIL_R * R and atr:
        trail = price - TRAIL_ATR * atr
        if trail > new_stop + MIN_STEP_ATR * atr and trail < price * (1 - MIN_GAP):
            new_stop = trail
            out["why"].append("подтяжка за ценой")
    target = partial_at if partial_at and partial_at > avg else avg * (1 + PARTIAL_PCT)
    if not partial_done and price >= target:
        out["partial"] = True
        out["why"].append("частичная фиксация")
        new_stop = max(new_stop, avg * (1 + fee))
    if new_stop > stop * (1 + 1e-6) and new_stop < price * (1 - MIN_GAP):
        out["stop"] = new_stop
    return out


class Manager:
    def __init__(self, broker, journal, client, notify=lambda t: None, candles_fn=None, now_fn=None):
        self.b, self.j, self.c, self.notify = broker, journal, client, notify
        self.candles = candles_fn or (lambda a: data.candles(a, "1h", **({"limit": 80} if a.kind == "crypto" else {"rng": "10d"})))
        self.now = now_fn or (lambda: dt.datetime.now(dt.timezone.utc))

    def _say(self, asset, key, text):
        """Не чаще раза в 30 минут по одному виду события на актив."""
        k, t = f"manage_msg:{asset}:{key}", self.now().timestamp()
        if t - (self.j.kv_get(k) or 0) > 1800:
            self.j.kv_set(k, t)
            self.notify(text)

    def run(self) -> list[str]:
        acts = []
        for name, p in list(self.j.positions().items()):
            try:
                acts += self._one(name, p)
            except AlpacaError as e:
                self.notify(report.service("task_fail", detail=f"сопровождение позиции {report.name(name)}"))
                acts.append(f"{name}: ошибка {str(e)[:100]}")
        return acts

    def _one(self, name, p) -> list[str]:
        a = config.BY_NAME[name]
        if self.j.kv_get(f"init_stop:{name}") is None:
            self.j.kv_set(f"init_stop:{name}", p["stop"])
        init = self.j.kv_get(f"init_stop:{name}")
        df = self.candles(a)
        price = float(self.c.price(a.alpaca))
        atr = indicators.snapshot(df)["atr14"] or 0.0
        opened = dt.datetime.fromisoformat(p["opened_ts"])
        age_h = (self.now() - opened).total_seconds() / 3600
        w = df[df.index >= self.now() - dt.timedelta(hours=48)]
        range48 = float((w["high"].max() - w["low"].min()) / p["avg"]) if len(w) >= 24 else None
        d = decide(p, price, atr, init, bool(self.j.kv_get(f"partial_done:{name}")), self.j.kv_get(f"partial_at:{name}"), age_h, range48, a.kind)
        out = []
        ts = self.now().isoformat(timespec="seconds")
        if d["close"]:
            r = self.b.sell(name, price, ts, d["close"])
            self.notify(report.manage_message("time", name, pnl=r["pnl"], pnl_pct=r["pnl"] / (p["avg"] * p["qty"])))
            return [f"{name}: закрыта по времени"]
        if d["partial"]:
            r = self.b.sell_part(name, PARTIAL_FRAC, ts, d["stop"] or p["stop"])
            if r:
                self.j.kv_set(f"partial_done:{name}", 1)
                self.notify(report.manage_message("partial", name, pnl=r["pnl"], price=r["price"], stop=max(d["stop"] or 0, p["stop"])))
                out.append(f"{name}: продана половина")
            return out
        if d["stop"]:
            if self.b.move_stop(name, d["stop"]):
                self._say(name, "stop", report.manage_message("stop", name, price=d["stop"], pct=d["stop"] / p["avg"] - 1, why=d["why"]))
                out.append(f"{name}: стоп → {d['stop']:.6g}")
        return out
