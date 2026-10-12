"""Точность ролей по шине board.db. Исход мнения — triple-barrier по 1h-свечам ПОСЛЕ отчёта:
верх +B, низ −B, B = ATR_1h·√H, горизонт H=24ч. buy верен, если первым достигнут верх; sell — низ; hold — если барьеры не задеты.
abstain не оценивается. Оцениваются роли с направленным мнением (аналитики, bull, bear, trader)."""
import math

import pandas as pd

H_BARS = 24


def barrier_outcome(after: pd.DataFrame, p0: float, atr: float, h: int = H_BARS) -> int | None:
    """+1 верх первым, −1 низ первым, 0 тайм-аут. None — горизонт ещё не наступил."""
    if not atr or atr <= 0:
        return None
    b = atr * math.sqrt(h)
    win = after.iloc[:h]
    for _, r in win.iterrows():
        up, dn = r["high"] >= p0 + b, r["low"] <= p0 - b
        if up and dn:
            return -1  # оба в одной свече — худший для лонга исход
        if up:
            return 1
        if dn:
            return -1
    return 0 if len(win) >= h else None


def settle(board, journal, candles_for, h: int = H_BARS) -> int:
    """candles_for(asset)->DataFrame 1h. Проставляет outcomes для созревших отчётов. Возвращает число новых."""
    runs = {r["mode"]: r for r in journal.q("SELECT mode, price, atr FROM runs")}  # runs.mode хранит cycle_id
    rows = board.q("""SELECT r.id, r.cycle_id, r.asset, r.ts, r.data FROM reports r LEFT JOIN outcomes x ON x.report_id=r.id WHERE x.report_id IS NULL""")
    import json
    cache, n = {}, 0
    for r in rows:
        d = json.loads(r["data"] or "{}")
        stance = d.get("stance") or d.get("action")
        run = runs.get(r["cycle_id"])
        if stance not in ("buy", "sell", "hold") or not run:
            continue
        try:
            df = cache.setdefault(r["asset"], candles_for(r["asset"]))
        except Exception:  # noqa: BLE001
            continue
        lab = barrier_outcome(df[df.index > pd.Timestamp(r["ts"])], run["price"], run["atr"], h)
        if lab is None:
            continue
        correct = int((stance == "buy" and lab == 1) or (stance == "sell" and lab == -1) or (stance == "hold" and lab == 0))
        board.x("INSERT INTO outcomes(report_id,label,correct) VALUES(?,?,?)", (r["id"], lab, correct))
        n += 1
    return n


def role_accuracy(board) -> list[dict]:
    return board.q("""SELECT r.role, COUNT(*) n, ROUND(AVG(x.correct),3) accuracy FROM reports r JOIN outcomes x ON x.report_id=r.id
                      GROUP BY r.role ORDER BY r.role""")
