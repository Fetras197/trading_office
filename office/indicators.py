"""Индикаторы на TA-Lib."""
import numpy as np
import pandas as pd
import talib as ta


def _f(x):
    return None if x is None or x != x else float(f"{float(x):.8g}")   # значащие цифры, а не знаки после запятой: у PEPE/SHIB цена ~1e-6, ATR при round(…, 6) обращался в 0


def snapshot(df: pd.DataFrame) -> dict:
    """Последние значения индикаторов (для промптов и скринера)."""
    c, h, l, v = (df[k].to_numpy(float) for k in ("close", "high", "low", "volume"))
    m, sig, hist = ta.MACD(c)
    up, mid, lo = ta.BBANDS(c, 20)
    out = {
        "close": c[-1], "ema20": ta.EMA(c, 20)[-1], "ema50": ta.EMA(c, 50)[-1], "ema200": ta.EMA(c, 200)[-1],
        "rsi14": ta.RSI(c)[-1], "macd": m[-1], "macd_signal": sig[-1], "macd_hist": hist[-1],
        "atr14": ta.ATR(h, l, c)[-1], "adx14": ta.ADX(h, l, c)[-1], "roc10": ta.ROC(c, 10)[-1],
        "bb_upper": up[-1], "bb_lower": lo[-1], "obv_slope10": np.polyfit(range(10), ta.OBV(c, v)[-10:], 1)[0] if len(c) > 30 else None,
        "ret_24bars": c[-1] / c[-25] - 1 if len(c) > 25 else None,
        "high_50": h[-50:].max(), "low_50": l[-50:].min(),
    }
    return {k: _f(x) for k, x in out.items()}
