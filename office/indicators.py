"""Детерминированные индикаторы на pandas. Вход: DataFrame с колонками open/high/low/close/volume."""
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, float("nan")))


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def macd(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    line = ema(s, 12) - ema(s, 26)
    return line, ema(line, 9)


def snapshot(df: pd.DataFrame) -> dict:
    """Последние значения индикаторов (для промпта и rule-based ролей)."""
    c = df["close"]
    m, sig = macd(c)
    sma20, sd20 = c.rolling(20).mean(), c.rolling(20).std()
    out = {
        "close": c.iloc[-1], "ema20": ema(c, 20).iloc[-1], "ema50": ema(c, 50).iloc[-1],
        "ema200": ema(c, 200).iloc[-1], "rsi14": rsi(c).iloc[-1], "macd": m.iloc[-1],
        "macd_signal": sig.iloc[-1], "macd_hist": (m - sig).iloc[-1], "atr14": atr(df).iloc[-1],
        "bb_upper": (sma20 + 2 * sd20).iloc[-1], "bb_lower": (sma20 - 2 * sd20).iloc[-1],
        "ret_24bars": c.iloc[-1] / c.iloc[-25] - 1 if len(c) > 25 else None,
        "high_50": df["high"].tail(50).max(), "low_50": df["low"].tail(50).min(),
    }
    return {k: (None if v is None or v != v else round(float(v), 6)) for k, v in out.items()}
