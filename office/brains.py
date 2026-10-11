"""«Мозги» фирмы: LLMBrain (11 ролей) и RuleBrain (детерминированный базлайн для бэктеста и shadow-режима)."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .llm import LLMError

STANCES = {"buy", "sell", "hold", "abstain", "n/a"}
ANALYSTS = ["tech_analyst", "fundamental_analyst", "news_sentiment", "onchain_derivs"]


@dataclass
class Opinion:
    role: str
    stance: str
    confidence: float
    rationale: str
    raw: dict | None = None


@dataclass
class Decision:
    action: str = "hold"          # buy | sell | hold
    entry: float | None = None
    stop: float | None = None
    take: float | None = None
    size_mult: float = 1.0
    reason: str = ""
    opinions: list[Opinion] = field(default_factory=list)


def _num(x, lo, hi, default=0.0):
    try:
        return min(hi, max(lo, float(x)))
    except (TypeError, ValueError):
        return default


def _bad(role, why, raw=None):
    """Невалидный ответ агента = ошибка → LLMError → цикл по активу пропускается (правило владельца)."""
    raise LLMError(f"{role}: {why}")


def normalize(role: str, raw: dict, trader_action: str = "hold") -> Opinion:
    """Валидация ответа роли → Opinion. Некорректное → LLMError (пропуск цикла по активу)."""
    try:
        if role in ANALYSTS:
            st = str(raw["stance"]).lower()
            if st not in STANCES - {"n/a"}:
                return _bad(role, f"bad stance {st!r}", raw)
            return Opinion(role, st, _num(raw.get("confidence"), 0, 1), str(raw.get("rationale", ""))[:400], raw)
        if role in ("bull_researcher", "bear_researcher"):
            cs = _num(raw["case_strength"], 0, 1)
            side = "buy" if role == "bull_researcher" else "sell"
            return Opinion(role, side if cs >= 0.5 else "hold", cs, str(raw.get("rationale", ""))[:400], raw)
        if role == "trader":
            a = str(raw["action"]).lower()
            if a not in ("buy", "sell", "hold"):
                return _bad(role, f"bad action {a!r}", raw)
            return Opinion(role, a, _num(raw.get("confidence"), 0, 1), str(raw.get("rationale", ""))[:400], raw)
        if role == "risk_manager":
            v = str(raw["verdict"]).lower()
            if v not in ("approve", "reduce", "reject"):
                return _bad(role, f"bad verdict {v!r}", raw)
            return Opinion(role, "hold" if v == "reject" else trader_action, 0.5, str(raw.get("rationale", ""))[:400], raw)
        if role == "portfolio_manager":
            d = str(raw["decision"]).lower()
            if d not in ("approve", "reject"):
                return _bad(role, f"bad decision {d!r}", raw)
            return Opinion(role, trader_action if d == "approve" else "hold", 0.5, str(raw.get("rationale", ""))[:400], raw)
        if role == "executor":
            return Opinion(role, "n/a", 0.0, str(raw.get("rationale", ""))[:400], raw)
    except (KeyError, TypeError) as e:
        return _bad(role, f"missing field {e}", raw)
    return _bad(role, "unknown role", raw)


class LLMBrain:
    def __init__(self, llm, lessons=None):
        self.llm, self.lessons = llm, lessons or (lambda: [])

    def _ask(self, role, payload, **kw) -> Opinion:
        for attempt in (1, 2):  # один повтор при сбое вызова/парсинга/схемы
            try:
                return normalize(role, self.llm.ask(role, payload), **kw)
            except LLMError:
                if attempt == 2:
                    raise

    def decide(self, bundle: dict, account: dict) -> Decision:
        """account: {equity, cash, position|None, entries_today, day_pnl}"""
        base = {"market": bundle, "account": account}
        with ThreadPoolExecutor(4) as ex:
            ops = list(ex.map(lambda r: self._ask(r, base), ANALYSTS))
        reports = {o.role: {"stance": o.stance, "confidence": o.confidence, "rationale": o.rationale} for o in ops}
        d = Decision(opinions=ops)
        if not any(o.stance in ("buy", "sell") and o.confidence >= 0.5 for o in ops):
            d.reason = "short-circuit: нет аналитика с directional-мнением ≥0.5"
            return d
        debate = {**base, "reports": reports}
        with ThreadPoolExecutor(2) as ex:
            bull, bear = ex.map(lambda r: self._ask(r, debate), ["bull_researcher", "bear_researcher"])
        ops += [bull, bear]
        debate["bull_case"], debate["bear_case"] = bull.raw, bear.raw
        tp = {**debate, "lessons": self.lessons(), "position": account.get("position")}
        trader = self._ask("trader", tp)
        ops.append(trader)
        if trader.stance not in ("buy", "sell") or not trader.raw:
            d.reason = f"трейдер: {trader.stance}"
            return d
        d.action = trader.stance
        t = trader.raw
        d.entry, d.stop, d.take = (_num(t.get(k), 0, 1e12, None) for k in ("entry", "stop", "take_profit"))
        if d.action == "sell":  # закрытие позиции снижает риск — без дополнительных согласований
            d.reason = "трейдер: закрыть позицию"
            return d
        proposal = {**tp, "proposal": t}
        rm = self._ask("risk_manager", proposal, trader_action="buy")
        pm_in = {**proposal, "risk_manager": rm.raw}
        pm = self._ask("portfolio_manager", pm_in, trader_action="buy")
        ops += [rm, pm]
        if rm.raw and str(rm.raw.get("verdict")).lower() == "reject":
            d.action, d.reason = "hold", "risk_manager: reject"
        elif pm.stance != "buy":
            d.action, d.reason = "hold", "portfolio_manager: reject" if pm.raw else "portfolio_manager недоступен"
        else:
            mult = _num(pm.raw.get("size_mult"), 0, 1, 1.0)
            if str(rm.raw.get("verdict")).lower() == "reduce":
                mult = min(mult, _num(rm.raw.get("size_mult"), 0, 1, 1.0))
            d.size_mult, d.reason = mult, "approved by PM"
        return d

    def preflight(self, ticket: dict) -> Opinion:
        return self._ask("executor", ticket)

    def digest(self, payload: dict) -> dict:
        try:  # журнал не торгует: сбой не критичен
            return self.llm.ask("journal", payload)
        except LLMError as e:
            return {"digest": f"journal недоступен: {e}", "lessons": []}


class RuleBrain:
    """Детерминированный базлайн: tech (EMA/MACD/RSI) + derivs (funding). fundamental/news → abstain (нет истории)."""
    name = "rule"

    def decide(self, bundle: dict, account: dict) -> Decision:
        s = bundle["indicators_1h"]
        s4 = bundle["indicators_4h"]
        ops = [self._tech(s, s4), self._derivs(bundle.get("derivatives", {}))]
        ops += [Opinion("fundamental_analyst", "abstain", 0.0, "нет данных", None),
                Opinion("news_sentiment", "abstain", 0.0, "нет данных", None)]
        tech, der = ops[0], ops[1]
        d = Decision(opinions=ops)
        price, atr = s["close"], s["atr14"]
        if tech.stance == "buy" and der.stance != "sell":
            d.action, d.entry, d.stop, d.take = "buy", price, price - 2.0 * atr, price + 3.5 * atr
        elif tech.stance == "sell" and account.get("position"):
            d.action = "sell"
        d.reason = f"tech={tech.stance} derivs={der.stance}"
        return d

    @staticmethod
    def _tech(s, s4) -> Opinion:
        score = 0
        score += 1 if s["ema20"] > s["ema50"] else -1
        score += 1 if s["close"] > s["ema200"] else -1
        score += 1 if s["macd_hist"] > 0 else -1
        score += 1 if s4["ema20"] > s4["ema50"] else -1
        rsi = s["rsi14"] or 50
        if score >= 3 and rsi < 70:
            return Opinion("tech_analyst", "buy", 0.5 + 0.1 * score / 2, f"score={score} rsi={rsi:.0f}")
        if score <= -3:
            return Opinion("tech_analyst", "sell", 0.5 + 0.1 * abs(score) / 2, f"score={score} rsi={rsi:.0f}")
        return Opinion("tech_analyst", "hold", 0.5, f"score={score} rsi={rsi:.0f}")

    @staticmethod
    def _derivs(dv) -> Opinion:
        f = dv.get("funding_hourly")
        if f is None:
            return Opinion("onchain_derivs", "abstain", 0.0, "нет данных")
        if f > 3e-5:
            return Opinion("onchain_derivs", "sell", 0.6, f"перегрев лонгов funding={f:.2e}")
        if f < -1e-5:
            return Opinion("onchain_derivs", "buy", 0.6, f"перегрев шортов funding={f:.2e}")
        return Opinion("onchain_derivs", "hold", 0.5, f"funding={f:.2e}")

    def preflight(self, ticket):
        return None
