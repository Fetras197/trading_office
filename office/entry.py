"""Правила входа (чистые функции, без LLM и сети). Лимиты риска здесь НЕ трогаются — их держит RiskGate:
2% риска на сделку, −5% в день, ≤5 входов в сутки, ≤3 позиций, без плеча и шортов.

Вход = технический сигнал (код, по 1h-индикаторам) И среди живых аналитиков «за» не меньше половины и «за» больше, чем «против».
Неполные данные (нет новостей/фундаментала, часть аналитиков воздержалась, меньше 3 голосов) — вход половинным объёмом, а не пропуск.
Режим TECH_ONLY: если аналитики не одобрили (или голосов нет), технический сигнал всё равно даёт вход ПОЛОВИННЫМ объёмом — пока «Картина дня» не наложила
запрет по активу с конкретной причиной и сроком. Против тренда (поддержка / возврат к средней) вход всегда половинным."""
from dataclasses import dataclass, field

STOP_ATR, TAKE_ATR = 2.0, 4.0          # стоп 2·ATR, тейк 4·ATR (R:R = 2)
CT_STOP_ATR, CT_TAKE_ATR = 1.5, 3.0    # акции, против тренда: стоп ближе, цель короче (R:R = 2)
# Крипта: комиссия+проскальзывание ≈0,6% за круг — это ~0,85 ATR(1ч). Бэктест 120 дн. по 32 парам Alpaca (сигналы всех 4 шаблонов, выход по стопу/тейку/72 ч, с издержками):
# при 2/4 ATR средний результат −0,26…−0,20 ATR на сделку у трёх шаблонов из четырёх; при 4/8 ATR +0,17…+0,79. Поэтому для крипты стоп 3,8 ATR / тейк 7,6 ATR
# (стоп ≤ max_stop_atr=4 из RiskGate; размер сделки считает гейт, лимит 25% счёта и риск ≤2% остаются).
CRYPTO_STOP_ATR, CRYPTO_TAKE_ATR = 3.8, 7.6
TECH_ONLY = True                       # режим «только технический сигнал» (см. выше)
COUNTER = ("support", "revert")        # шаблоны против тренда: всегда половинный объём
TECH_RU = {"lab": "стратегия лаборатории", "trend": "тренд", "bounce": "отскок в росте", "support": "отскок от поддержки", "revert": "возврат к средней"}
HALF = 0.5                             # множитель объёма при неполных данных
MIN_LIVE_FULL = 3                      # меньше стольких живых голосов — данные считаются неполными
RSI_MAX, RSI_MIN_TREND, RSI_BOUNCE = 75.0, 45.0, 38.0
COOLDOWN_H = 4.0                       # после стопа по активу повторный вход не раньше, чем через столько часов


@dataclass
class Tech:
    signal: bool
    kind: str = ""          # trend | bounce | ""
    score: float = 0.0      # 0..1 — доля выполненных условий лучшего из шаблонов
    notes: list[str] = field(default_factory=list)   # что не выполнено (по-русски)


def technical(ind: dict) -> Tech:
    """Сигнал на покупку по 1h-индикаторам (ind = indicators.snapshot). Четыре шаблона, любой даёт сигнал.
    trend:  цена выше EMA50, EMA20 выше EMA50, гистограмма MACD > 0, RSI в 45–75 (не слабый и не перегретый).
    bounce: цена выше EMA200 (долгий тренд цел), RSI ≤ 38 (перепродан), цена не ниже нижней полосы Боллинджера.
    support (боковик/падение): цена в пределах 1 ATR над минимумом 50 свечей, RSI ≤ 45 — отскок от поддержки.
    revert (боковик/падение): цена ниже EMA20 не менее чем на 1,5 ATR, RSI ≤ 35 и цена у нижней полосы Боллинджера — возврат к средней."""
    g = lambda k: ind.get(k)  # noqa: E731
    need = ("close", "ema20", "ema50", "ema200", "rsi14", "macd_hist")
    if any(g(k) is None for k in need):
        return Tech(False, notes=["не хватает данных для индикаторов"])
    c, rsi = g("close"), g("rsi14")
    trend = [("цена ниже средней 50", c > g("ema50")), ("короткая средняя ниже длинной", g("ema20") > g("ema50")),
             ("импульс (MACD) отрицательный", g("macd_hist") > 0), (f"RSI {rsi:.0f} вне 45–75", RSI_MIN_TREND <= rsi <= RSI_MAX)]
    bounce = [("цена ниже средней 200", c > g("ema200")), (f"RSI {rsi:.0f} выше 38 — нет перепроданности", rsi <= RSI_BOUNCE),
              ("цена пробила нижнюю полосу", g("bb_lower") is None or c >= g("bb_lower"))]
    atr, low50, e20, bbl = g("atr14"), g("low_50"), g("ema20"), g("bb_lower")
    pats = {"trend": trend, "bounce": bounce}
    if atr and low50 is not None:   # шаблоны для бокового и падающего рынка (не требуют роста)
        pats["support"] = [("цена далеко от поддержки (минимума 50 свечей)", 0 <= c - low50 <= 1.0 * atr), (f"RSI {rsi:.0f} выше 45 — нет слабости", rsi <= 45.0)]
        pats["revert"] = [("цена не ушла от средней на 1,5 ATR", e20 - c >= 1.5 * atr), (f"RSI {rsi:.0f} выше 35", rsi <= 35.0),
                          ("цена выше нижней полосы Боллинджера", bbl is None or c <= bbl + 0.25 * atr)]
    score = {k: sum(ok for _, ok in v) / len(v) for k, v in pats.items()}
    for k in ("trend", "bounce", "support", "revert"):
        if k in pats and score[k] == 1:
            return Tech(True, k, 1.0)
    k = max(score, key=score.get)
    return Tech(False, score=score[k], notes=[n for n, ok in pats[k] if not ok])


@dataclass
class Votes:
    buy: int = 0
    sell: int = 0
    hold: int = 0
    abstain: int = 0

    @property
    def live(self) -> int:
        return self.buy + self.sell + self.hold

    @property
    def share(self) -> float:
        return self.buy / self.live if self.live else 0.0


def count_votes(analysts: list[dict]) -> Votes:
    v = Votes()
    for r in analysts:
        s = r.get("stance")
        if s in ("buy", "sell", "hold"):
            setattr(v, s, getattr(v, s) + 1)
        else:
            v.abstain += 1
    return v


def majority(v: Votes) -> bool:
    """«За» не меньше половины живых голосов и строго больше, чем «против»."""
    return v.live > 0 and v.buy * 2 >= v.live and v.buy > v.sell


def data_gaps(bundle: dict, votes: Votes) -> list[str]:
    """Чего не хватило (по-русски) — основание для половинного объёма."""
    gaps = []
    if not bundle.get("news"):
        gaps.append("нет свежих новостей")
    fu = bundle.get("fundamentals") or {}
    if not fu or "error" in fu:
        gaps.append("нет фундаментальных данных")
    if votes.abstain:
        gaps.append(f"{votes.abstain} из аналитиков воздержались")
    if 0 < votes.live < MIN_LIVE_FULL:
        gaps.append(f"мало голосов ({votes.live})")
    return gaps


def levels(price: float, atr: float, kind: str = "trend", asset_kind: str = "stock") -> tuple[float, float]:
    if asset_kind == "crypto":
        s, t = CRYPTO_STOP_ATR, CRYPTO_TAKE_ATR
    else:
        s, t = (CT_STOP_ATR, CT_TAKE_ATR) if kind in COUNTER else (STOP_ATR, TAKE_ATR)
    return price - s * atr, price + t * atr


def proximity(tech: Tech, votes: Votes | None) -> float:
    """Насколько кандидат близок ко входу (0..1) — для отчёта «Почему не вошли». Сигнал и голоса дают по половине."""
    t = 0.5 * (1.0 if tech.signal else tech.score * 0.8)
    if votes is None or not votes.live:
        return t
    return t + 0.5 * min(1.0, votes.share / 0.5) * (1.0 if votes.buy >= votes.sell else 0.6)


# ---------- экономика сделки (v5): цель должна окупать издержки ----------
MAX_SPREAD = 0.003         # спред шире 0,3% — вход запрещён (круг купить-продать дороже любой разумной цели)
MIN_REWARD_RISK = 2.0      # цель с комиссиями ≥ 2× риска с комиссиями (все активы)
MIN_TARGET_FEES = 4.0      # крипта: цель (грязная) ≥ 4× комиссии круга
MAX_TAKE_ATR = 16.0        # дальше этого тейк не ищем — сделка не стоит риска


@dataclass
class Plan:
    ok: bool
    stop: float = 0.0
    take: float = 0.0
    risk_unit: float = 0.0       # убыток на единицу до стопа С комиссиями и проскальзыванием
    reward_unit: float = 0.0     # прибыль на единицу при тейке ПОСЛЕ комиссий и проскальзывания
    fees_unit: float = 0.0       # комиссии круга (вход+выход) на единицу
    reason: str = ""


def _units(price: float, stop: float, take: float, costs) -> tuple[float, float, float]:
    eff = price * (1 + costs.slippage)
    risk = eff * (1 + costs.fee) - stop * (1 - costs.slippage) * (1 - costs.fee)
    reward = take * (1 - costs.slippage) * (1 - costs.fee) - eff * (1 + costs.fee)
    return risk, reward, costs.fee * (price + take)


def trade_plan(price: float, atr: float, pattern: str, asset_kind: str, costs) -> Plan:
    """Стоп и тейк + проверка экономики. Крипта: стоп 3,8 ATR; тейк — не ближе 7,6 ATR и такой, чтобы цель с комиссиями была ≥ 2× риска с комиссиями
    И цель (грязная) ≥ 4× комиссии круга; ищется шагом 0,25 ATR до MAX_TAKE_ATR, иначе отказ. Акции/ETF: уровни 2/4 ATR (против тренда 1,5/3), комиссия 0 — действует только правило 2× (тейк при необходимости отодвигается так же)."""
    if not atr > 1e-9:   # 0/NaN/ничтожный ATR: тейк не сдвигается, цикл не кончится
        return Plan(False, reason="экономика сделки не сходится: ATR нулевой")
    stop, take = levels(price, atr, pattern, asset_kind)
    step = 0.25 * atr
    while True:
        risk, reward, fees = _units(price, stop, take, costs)
        ok_rr = reward >= MIN_REWARD_RISK * risk
        ok_fee = asset_kind != "crypto" or (take - price) >= MIN_TARGET_FEES * fees
        if ok_rr and ok_fee:
            return Plan(True, stop, take, risk, reward, fees)
        if (take - price) / atr >= MAX_TAKE_ATR:
            why = (f"цель с комиссиями {reward / risk:.1f}× риска (нужно ≥ {MIN_REWARD_RISK:g}×)" if not ok_rr else
                   f"цель всего {(take - price) / fees:.1f}× комиссии круга (нужно ≥ {MIN_TARGET_FEES:g}×)")
            return Plan(False, stop, take, risk, reward, fees, "экономика сделки не сходится: " + why + (f"; тейк дальше {MAX_TAKE_ATR:g} ATR не ищем"))
        take += step
