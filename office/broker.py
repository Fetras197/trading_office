"""Paper-брокер (симулятор, только бэктест). Long-only, одна позиция на актив, стоп/тейк проверяются по high/low свечей.
Комиссия и проскальзывание из Costs. Состояние (cash, позиции, сделки) — в Journal."""
from .config import COSTS, START_CASH, Costs


class SimBroker:
    def __init__(self, journal, kinds: dict[str, str], costs: dict[str, Costs] | None = None, start_cash: float = START_CASH):
        """kinds: имя актива → crypto|stock; costs: kind → Costs (по умолчанию COSTS)."""
        self.j, self.kinds, self.costs = journal, kinds, costs or COSTS
        if self.j.kv_get("cash") is None:
            self.j.kv_set("cash", start_cash)
            self.j.kv_set("start_cash", start_cash)

    @property
    def cash(self) -> float:
        return self.j.kv_get("cash")

    def position(self, asset):
        return self.j.positions().get(asset)

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(p["qty"] * prices.get(a, p["avg"]) for a, p in self.j.positions().items())

    def buy(self, asset, qty, ref_price, stop, take, ts, run_id=None):
        if self.position(asset):
            raise ValueError(f"позиция {asset} уже открыта")
        c = self.costs[self.kinds[asset]]
        price = ref_price * (1 + c.slippage)
        cost = qty * price
        fee = cost * c.fee
        if cost + fee > self.cash + 1e-9:
            raise ValueError("недостаточно cash")
        self.j.kv_set("cash", self.cash - cost - fee)
        self.j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts) VALUES(?,?,?,?,?,?,?,?)",
                  (asset, qty, price, stop, take, fee, run_id, ts))
        self.j.add_fill(ts, asset, "buy", qty, price, fee, "entry", run_id)
        return {"price": price, "fee": fee, "qty": qty}

    def _close(self, asset, price, ts, reason):
        p = self.position(asset)
        proceeds = p["qty"] * price
        fee = proceeds * self.costs[self.kinds[asset]].fee
        self.j.kv_set("cash", self.cash + proceeds - fee)
        fees = p["fees"] + fee
        pnl = proceeds - p["qty"] * p["avg"] - fees
        risk = p["qty"] * (p["avg"] - p["stop"])
        self.j._x("DELETE FROM positions WHERE asset=?", (asset,))
        self.j._x("INSERT INTO trades(asset,entry_ts,exit_ts,entry,exit,qty,fees,pnl,r_mult,run_id,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (asset, p["opened_ts"], ts, p["avg"], price, p["qty"], fees, pnl, pnl / risk if risk > 0 else None,
                   p["run_id"], reason))
        self.j.add_fill(ts, asset, "sell", p["qty"], price, fee, reason, p["run_id"])
        return {"price": price, "fee": fee, "pnl": pnl, "reason": reason}

    def sell(self, asset, ref_price, ts, reason="signal"):
        if not self.position(asset):
            raise ValueError(f"нет позиции {asset}")
        return self._close(asset, ref_price * (1 - self.costs[self.kinds[asset]].slippage), ts, reason)

    def check_exits(self, asset, high, low, ts):
        """Стоп и тейк по диапазону свечей с прошлой проверки. Если задеты оба — консервативно считаем, что стоп первым."""
        p = self.position(asset)
        if not p:
            return None
        if low <= p["stop"]:
            return self._close(asset, p["stop"] * (1 - self.costs[self.kinds[asset]].slippage), ts, "stop")
        if p["take"] and high >= p["take"]:
            return self._close(asset, p["take"], ts, "take")
        return None
