"""RiskGate — жёсткие лимиты в коде. LLM может только предлагать; гейт режет размер или отклоняет.
Стоп никогда не двигается (иначе ломается R:R): размер позиции подгоняется под стоп."""
import math
from dataclasses import dataclass

from .config import Costs, Limits


@dataclass
class Verdict:
    ok: bool
    qty: float = 0.0
    reason: str = ""


class RiskGate:
    def __init__(self, limits: Limits = Limits()):
        self.l = limits

    def check_entry(self, *, kind, costs: Costs, entry, stop, atr, equity, day_start_equity, entries_today,
                    open_positions, cash, has_position, size_mult=1.0) -> Verdict:
        l = self.l
        if has_position:
            return Verdict(False, reason="позиция по активу уже открыта")
        if self.trading_halted(equity, day_start_equity):
            return Verdict(False, reason=f"дневной стоп {l.daily_stop:.0%} достигнут")
        if entries_today >= l.max_trades_day:
            return Verdict(False, reason=f"лимит {l.max_trades_day} сделок/день")
        if open_positions >= l.max_positions:
            return Verdict(False, reason=f"лимит {l.max_positions} позиций")
        if not (entry and stop and 0 < stop < entry):
            return Verdict(False, reason="стоп обязателен и должен быть ниже входа (long-only)")
        dist = entry - stop
        if atr and not (l.min_stop_atr * atr <= dist <= l.max_stop_atr * atr):
            return Verdict(False, reason=f"стоп вне диапазона {l.min_stop_atr}-{l.max_stop_atr} ATR")
        eff_entry = entry * (1 + costs.slippage)  # реальная цена входа хуже; риск считаем от неё
        # полный убыток на единицу при срабатывании стопа: вход+комиссия минус выход по стопу (с проскальзыванием) за вычетом комиссии
        risk_dist = eff_entry * (1 + costs.fee) - stop * (1 - costs.slippage) * (1 - costs.fee)
        qty = min(l.risk_per_trade * equity / risk_dist,
                  l.max_notional * equity / eff_entry,
                  cash / (eff_entry * (1 + costs.fee))) * min(1.0, max(0.0, size_mult))
        qty = math.floor(qty) if kind == "stock" else math.floor(qty * 1e6) / 1e6  # акции: целые (bracket не поддерживает дроби)
        if qty * eff_entry < l.min_notional_usd or qty <= 0:
            return Verdict(False, reason="размер позиции ниже минимума")
        return Verdict(True, qty=qty, reason="ok")

    @staticmethod
    def planned_risk(costs: Costs, entry: float, stop: float, qty: float) -> float:
        """Заложенный убыток до стопа в $ ВМЕСТЕ с комиссиями и проскальзыванием (та же формула, по которой гейт считает размер). Именно эта цифра показывается в сообщениях."""
        eff = entry * (1 + costs.slippage)
        return max(eff * (1 + costs.fee) - stop * (1 - costs.slippage) * (1 - costs.fee), 0.0) * qty

    def trading_halted(self, equity, day_start_equity) -> bool:
        return day_start_equity > 0 and equity / day_start_equity - 1 <= self.l.daily_stop
