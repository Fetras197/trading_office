"""Правила входа (чистые функции, без LLM и сети). Лимиты риска здесь НЕ трогаются — их держит RiskGate:
2% риска на сделку, −5% в день, ≤5 входов в сутки, ≤3 позиций, без плеча и шортов.

Вход = технический сигнал (код, по 1h-индикаторам) И среди живых аналитиков «за» не меньше половины и «за» больше, чем «против».
Неполные данные (нет новостей/фундаментала, часть аналитиков воздержалась, меньше 3 голосов) — вход половинным объёмом, а не пропуск.
Нет ни одного живого голоса (Gemini недоступен / бюджет исчерпан) — не входим."""
from dataclasses import dataclass, field

STOP_ATR, TAKE_ATR = 2.0, 4.0          # стоп 2·ATR, тейк 4·ATR (R:R = 2)
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
    """Сигнал на покупку по 1h-индикаторам (ind = indicators.snapshot).
    trend:  цена выше EMA50, EMA20 выше EMA50, гистограмма MACD > 0, RSI в 45–75 (не слабый и не перегретый).
    bounce: цена выше EMA200 (долгий тренд цел), RSI ≤ 38 (перепродан), цена не ниже нижней полосы Боллинджера."""
    g = lambda k: ind.get(k)  # noqa: E731
    need = ("close", "ema20", "ema50", "ema200", "rsi14", "macd_hist")
    if any(g(k) is None for k in need):
        return Tech(False, notes=["не хватает данных для индикаторов"])
    c, rsi = g("close"), g("rsi14")
    trend = [("цена ниже средней 50", c > g("ema50")), ("короткая средняя ниже длинной", g("ema20") > g("ema50")),
             ("импульс (MACD) отрицательный", g("macd_hist") > 0), (f"RSI {rsi:.0f} вне 45–75", RSI_MIN_TREND <= rsi <= RSI_MAX)]
    bounce = [("цена ниже средней 200", c > g("ema200")), (f"RSI {rsi:.0f} выше 38 — нет перепроданности", rsi <= RSI_BOUNCE),
              ("цена пробила нижнюю полосу", g("bb_lower") is None or c >= g("bb_lower"))]
    st, sb = sum(ok for _, ok in trend) / len(trend), sum(ok for _, ok in bounce) / len(bounce)
    if st == 1:
        return Tech(True, "trend", 1.0)
    if sb == 1:
        return Tech(True, "bounce", 1.0)
    best, pat = (trend, st) if st >= sb else (bounce, sb)
    return Tech(False, score=pat, notes=[n for n, ok in best if not ok])


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


def levels(price: float, atr: float) -> tuple[float, float]:
    return price - STOP_ATR * atr, price + TAKE_ATR * atr


def proximity(tech: Tech, votes: Votes | None) -> float:
    """Насколько кандидат близок ко входу (0..1) — для отчёта «Почему не вошли». Сигнал и голоса дают по половине."""
    t = 0.5 * (1.0 if tech.signal else tech.score * 0.8)
    if votes is None or not votes.live:
        return t
    return t + 0.5 * min(1.0, votes.share / 0.5) * (1.0 if votes.buy >= votes.sell else 0.6)
