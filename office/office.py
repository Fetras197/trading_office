"""Один шаг решения по активу: данные → мозг → RiskGate → (одобрение) → брокер → журнал. Общий для live и бэктеста."""
import datetime as dt
from dataclasses import dataclass

from .config import COSTS
from .llm import LLMError
from .risk import RiskGate


@dataclass
class StepResult:
    asset: str
    status: str   # skipped | hold | rejected | declined | opened | closed | error
    detail: str = ""


class Office:
    def __init__(self, brain, broker, journal, gate: RiskGate | None = None, approve=None, shadow=None, mode="live"):
        """approve(asset, ticket)->bool|None (None = нет ответа → отклонить). shadow — rule-brain для сравнения."""
        self.brain, self.broker, self.j = brain, broker, journal
        self.gate, self.approve, self.shadow, self.mode = gate or RiskGate(), approve, shadow, mode

    # --- состояние дня ---
    def day_start_equity(self, ts: str, equity: float) -> float:
        k = f"day_start:{ts[:10]}"
        v = self.j.kv_get(k)
        if v is None:
            self.j.kv_set(k, equity)
            v = equity
        return v

    def account(self, asset, ts, prices) -> dict:
        eq = self.broker.equity(prices)
        ds = self.day_start_equity(ts, eq)
        pos = self.broker.position(asset.name)
        return {"equity": eq, "cash": self.broker.cash, "day_start_equity": ds, "day_pnl_pct": round(eq / ds - 1, 4) if ds else 0,
                "entries_today": self.j.entries_on(ts[:10]), "open_positions": len(self.j.positions()),
                "position": {k: pos[k] for k in ("qty", "avg", "stop", "take")} if pos else None}

    # --- шаг ---
    def step(self, asset, bundle: dict, ts: str, prices: dict, lessons=None) -> StepResult:
        s1 = bundle["indicators_1h"]
        price, atr = s1["close"], s1["atr14"]
        run_id = self.j.add_run(ts, asset.name, price, atr, self.mode)
        acct = self.account(asset, ts, prices)
        if self.shadow:  # shadow-базлайн: пишем мнения для сравнения, на торговлю не влияет
            for o in self.shadow.decide(bundle, acct).opinions:
                self.j.add_opinion(run_id, ts, asset.name, f"rule:{o.role}", o.stance, o.confidence, o.rationale, shadow=1)
        try:
            d = self.brain.decide(bundle, acct)
        except Exception as e:  # noqa: BLE001 — любая ошибка/невалидный ответ агента → пропуск цикла по активу (в журнал)
            msg = str(e) if isinstance(e, LLMError) else f"{type(e).__name__}: {e}"
            self.j.set_summary(run_id, f"SKIPPED: {msg}")
            return StepResult(asset.name, "skipped", msg)
        for o in d.opinions:
            self.j.add_opinion(run_id, ts, asset.name, o.role, o.stance, o.confidence, o.rationale, o.raw)
        self.j.set_summary(run_id, d.reason)
        if d.action == "hold":
            self.j.add_decision(run_id, ts, asset.name, "hold", None, None, None, None, "n/a", d.reason)
            return StepResult(asset.name, "hold", d.reason)
        if d.action == "sell":
            if not acct["position"]:
                return StepResult(asset.name, "hold", "sell без позиции — игнор")
            did = self.j.add_decision(run_id, ts, asset.name, "sell", price, None, None, acct["position"]["qty"], "ok", d.reason, approved=1)
            r = self.broker.sell(asset.name, price, ts, "signal")
            self.j.update_decision(did, executed=1)
            return StepResult(asset.name, "closed", f"pnl={r['pnl']:.2f}")
        # buy
        costs = COSTS[asset.kind]
        v = self.gate.check_entry(kind=asset.kind, costs=costs, entry=d.entry or price, stop=d.stop, atr=atr, equity=acct["equity"],
                                  day_start_equity=acct["day_start_equity"], entries_today=acct["entries_today"],
                                  open_positions=acct["open_positions"], cash=acct["cash"], has_position=bool(acct["position"]),
                                  size_mult=d.size_mult)
        did = self.j.add_decision(run_id, ts, asset.name, "buy", d.entry or price, d.stop, d.take, v.qty, "ok" if v.ok else "rejected", v.reason)
        if not v.ok:
            return StepResult(asset.name, "rejected", v.reason)
        ticket = {"asset": asset.name, "side": "buy", "qty": v.qty, "entry": price, "stop": d.stop, "take": d.take,
                  "notional": round(v.qty * price, 2), "cash": acct["cash"], "equity": acct["equity"], "as_of": bundle.get("as_of")}
        try:
            pf = self.brain.preflight(ticket)
        except LLMError as e:
            self.j.update_decision(did, verdict="skipped", reason=f"executor: {e}")
            return StepResult(asset.name, "skipped", f"executor: {e}")
        if pf is not None and pf.raw and pf.raw.get("ticket_ok") is False:
            self.j.update_decision(did, verdict="rejected", reason=f"executor: {pf.raw.get('issues')}")
            return StepResult(asset.name, "rejected", f"executor: {pf.raw.get('issues')}")
        if self.approve:
            ok = self.approve(asset.name, ticket, d.reason)
            self.j.update_decision(did, approved=1 if ok else 0)
            if not ok:
                return StepResult(asset.name, "declined", "не одобрено" if ok is False else "нет ответа — отклонено")
        try:
            self.broker.buy(asset.name, v.qty, price, d.stop, d.take, ts, run_id)
        except Exception as e:  # noqa: BLE001 — брокерская ошибка не должна валить цикл остальных активов
            self.j.update_decision(did, verdict="error", reason=str(e)[:200])
            return StepResult(asset.name, "error", str(e)[:200])
        self.j.update_decision(did, executed=1)
        return StepResult(asset.name, "opened", f"qty={v.qty} stop={d.stop} take={d.take}")


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
