"""Точность по ролям. Исход мнения — triple-barrier по свечам ПОСЛЕ мнения:
верхний барьер +B, нижний −B, B = ATR_1h·√H (≈ дневная σ), горизонт H часов (по умолч. 24).
buy верен, если первым достигнут верх; sell — если низ; hold — если за H барьеры не задеты. abstain/n/a не оцениваются."""
import math

import pandas as pd

H_BARS = 24


def barrier_outcome(after: pd.DataFrame, p0: float, atr: float, h: int = H_BARS) -> int | None:
    """+1 верх первым, −1 низ первым, 0 тайм-аут. None — данных ещё недостаточно (горизонт не наступил)."""
    if not atr or atr <= 0:
        return None
    b = atr * math.sqrt(h)
    win = after.iloc[:h]
    for _, r in win.iterrows():
        up, dn = r["high"] >= p0 + b, r["low"] <= p0 - b
        if up and dn:
            return -1  # консервативно: оба в одной свече — считаем худший для лонга исход
        if up:
            return 1
        if dn:
            return -1
    return 0 if len(win) >= h else None


def settle(journal, candles_for, h: int = H_BARS) -> int:
    """candles_for(asset_name) -> DataFrame 1h. Проставляет outcomes для созревших мнений. Возвращает число новых."""
    rows = journal.q("""SELECT o.id oid, o.asset, o.stance, o.ts, r.price, r.atr FROM opinions o JOIN runs r ON r.id=o.run_id
                        LEFT JOIN outcomes x ON x.opinion_id=o.id WHERE x.opinion_id IS NULL AND o.stance IN ('buy','sell','hold')""")
    cache, n = {}, 0
    for r in rows:
        df = cache.setdefault(r["asset"], candles_for(r["asset"]))
        after = df[df.index > pd.Timestamp(r["ts"])]
        lab = barrier_outcome(after, r["price"], r["atr"], h)
        if lab is None:
            continue
        correct = int((r["stance"] == "buy" and lab == 1) or (r["stance"] == "sell" and lab == -1) or (r["stance"] == "hold" and lab == 0))
        journal._x("INSERT INTO outcomes(opinion_id,label,correct) VALUES(?,?,?)", (r["oid"], lab, correct))
        n += 1
    return n


def role_accuracy(journal, shadow=None) -> list[dict]:
    """Таблица по ролям: n, доля верных, отдельно по directional (buy/sell) мнениям."""
    sh = "" if shadow is None else f"AND o.shadow={int(shadow)}"
    return journal.q(f"""SELECT o.role, COUNT(*) n, ROUND(AVG(x.correct),3) accuracy,
        SUM(o.stance IN ('buy','sell')) n_dir,
        ROUND(AVG(CASE WHEN o.stance IN ('buy','sell') THEN x.correct END),3) accuracy_dir,
        ROUND(1.0*SUM(CASE WHEN o.stance IN ('buy','sell') THEN x.correct END)
              / NULLIF(SUM(CASE WHEN o.stance IN ('buy','sell') AND x.label!=0 THEN 1 END),0),3) hit_rate_dir
        FROM opinions o JOIN outcomes x ON x.opinion_id=o.id WHERE 1=1 {sh} GROUP BY o.role ORDER BY o.role""")
