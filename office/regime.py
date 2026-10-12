"""Режим рынка (3.1): trend / sideways / high_vol / panic — по дневным барам BTC (крипта) и SPY + VIX (акции). Считается кодом, без LLM.
Что меняется под режим: размер позиции (множитель — только уменьшает), подсказка по стилю входа трейдеру, априорные множители весов ролей;
в панике новые входы запрещены. Те же метки дней используются лабораторией (результат стратегии по режимам)."""
import pandas as pd
import talib as ta

REGIMES = ("trend", "sideways", "high_vol", "panic")
SIZE_MULT = {"trend": 1.0, "sideways": 0.75, "high_vol": 0.5, "panic": 0.0}
RU = {"trend": "тренд", "sideways": "боковик", "high_vol": "высокая волатильность", "panic": "паника"}
GUIDANCE = {"trend": "Trend: favour breakouts and pullbacks in the direction of the trend; wider take-profit is fine.",
            "sideways": "Sideways: avoid breakout chasing; prefer entries near range support with tight, near take-profit; skip weak signals.",
            "high_vol": "High volatility: smaller size (enforced), wider stops measured in ATR, demand stronger confirmation, avoid entries right after spikes.",
            "panic": "Panic: no new entries (enforced). Only manage/close existing positions."}
# априорные множители веса ролей по режиму (не накапливаются, не заменяют вес по заслугам: итоговый вес = вес по заслугам × априор)
ROLE_PRIOR = {"trend": {"market": 1.2, "quant": 1.0, "news": 0.9, "lab": 1.1},
              "sideways": {"market": 0.9, "quant": 1.2, "lab": 1.0},
              "high_vol": {"news": 1.2, "quant": 1.1, "fundamentals": 0.8, "market": 0.9},
              "panic": {"news": 1.2}}
THRESH = {"crypto": {"ret1": -0.08, "dd7": -0.15}, "stock": {"ret1": -0.03, "dd7": -0.07}}


def classify(df: pd.DataFrame, kind: str, vix: float | None = None) -> dict:
    """df — дневные бары (close/high/low). vix — последнее значение (для акций)."""
    c = df["close"].astype(float)
    r = c.pct_change().dropna()
    ret1, dd7 = float(c.iloc[-1] / c.iloc[-2] - 1), float(c.iloc[-1] / c.iloc[-8:].max() - 1)
    roll = r.rolling(20).std().dropna()
    vol_ratio = float(roll.iloc[-1] / roll.tail(120).median()) if len(roll) > 30 and roll.tail(120).median() else 1.0
    adx = float(ta.ADX(df["high"].to_numpy(float), df["low"].to_numpy(float), c.to_numpy(float), 14)[-1])
    th, why = THRESH[kind], []
    if ret1 <= th["ret1"]:
        why.append(f"падение за день {ret1:.1%}")
    if dd7 <= th["dd7"]:
        why.append(f"просадка от максимума недели {dd7:.1%}")
    if kind == "stock" and vix is not None and vix >= 35:
        why.append(f"VIX {vix:.0f}")
    if why:
        reg = "panic"
    elif vol_ratio > 1.5 or (kind == "stock" and vix is not None and vix >= 25):
        reg, why = "high_vol", [f"волатильность в {vol_ratio:.1f} раза выше обычной"] + ([f"VIX {vix:.0f}"] if vix else [])
    elif adx >= 25:
        reg, why = "trend", [f"сила тренда (ADX) {adx:.0f}"]
    else:
        reg, why = "sideways", [f"тренд слабый (ADX {adx:.0f})"]
    return {"regime": reg, "adx": round(adx, 1), "vol_ratio": round(vol_ratio, 2), "dd7": round(dd7, 4), "ret1": round(ret1, 4), "vix": vix, "why": why}


def labels(df: pd.DataFrame) -> pd.Series:
    """Метка режима по каждому дню истории (для лаборатории): панику по дню не размечаем отдельно — резкие падения идут в high_vol."""
    c = df["close"].astype(float)
    adx = pd.Series(ta.ADX(df["high"].to_numpy(float), df["low"].to_numpy(float), c.to_numpy(float), 14), index=df.index)
    vol = c.pct_change().rolling(20).std()
    ratio = vol / vol.rolling(120, min_periods=30).median()
    out = pd.Series("sideways", index=df.index)
    out[adx >= 25] = "trend"
    out[ratio > 1.5] = "high_vol"
    return out.where(adx.notna())


def current(board, kind: str, max_age_h: float = 3.0) -> dict | None:
    rep = board.latest("regime", max_age_h=max_age_h)
    return (rep["data"].get(kind) if rep else None)


def prior(regime: str | None, role: str) -> float:
    return ROLE_PRIOR.get(regime or "", {}).get(role, 1.0)


def snapshot(candles_fn, vix_fn) -> dict:
    """{'crypto': {...}, 'stock': {...}} — ошибки по одному рынку не мешают другому."""
    from .config import make_asset
    out = {}
    for kind, sym in (("crypto", "BTC/USD"), ("stock", "SPY")):
        try:
            vix = None
            if kind == "stock":
                try:
                    vix = float(vix_fn())
                except Exception:  # noqa: BLE001
                    vix = None
            out[kind] = classify(candles_fn(make_asset(sym)), kind, vix)
        except Exception as e:  # noqa: BLE001
            out[kind] = None
            out[f"{kind}_error"] = type(e).__name__
    return out


def mean_returns_by_regime(daily_returns: pd.Series, lab: pd.Series) -> dict:
    """Средняя дневная доходность стратегии и число дней по режимам (для лаборатории)."""
    j = pd.concat([daily_returns.rename("r"), lab.rename("g")], axis=1).dropna()
    return {g: {"days": int(len(x)), "mean": round(float(x["r"].mean()), 6)} for g, x in j.groupby("g") if len(x)}


