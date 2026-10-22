"""База для «за день» в сообщениях: эквити на момент ПОСЛЕДНЕГО ВЕЧЕРНЕГО ОТЧЁТА (по Минску), а не last_equity брокера (закрытие биржи США 16:00 по Нью-Йорку).
Брокер считает «за день» от закрытия прошлого торгового дня, из-за чего круглосуточная крипта и вечерние движения давали цифры, не совпадающие с вечерним отчётом
(инцидент 22.10: счёт 99 630 $ показан как −0,92% за день от 100 554 $, а вечером было 99 312 $).
Хранится в journal.kv['day_base'] = {day, equity, prev}. В вечернем отчёте сравниваем с ПРЕДЫДУЩИМ вечером; после отправки база сдвигается (раз в местные сутки — повторный запуск отчёта её не двигает).
Это только показ: лимиты риска (дневной стоп) считаются отдельно и не затронуты."""
import datetime as dt

KEY = "day_base"


def seed(j) -> dict | None:
    """Базы ещё нет: берём эквити журнала ближе всего к последнему вечернему отчёту (job:evening:last), если он был."""
    last = j.kv_get("job:evening:last")
    if not last:
        return None
    t = dt.datetime.fromisoformat(last)
    rows = j.q("SELECT ts, equity FROM equity ORDER BY ts")
    best = None
    for r in rows:
        d = abs((dt.datetime.fromisoformat(r["ts"]) - t).total_seconds())
        if d <= 1800 and (best is None or d < best[0]):
            best = (d, r["equity"])
    if not best:
        return None
    base = {"day": t.date().isoformat(), "equity": float(best[1]), "prev": None}
    j.kv_set(KEY, base)
    return base


def get(j, today: str, evening: bool = False) -> float | None:
    """Эквити базы или None (тогда показываются цифры брокера). today — местная дата (YYYY-MM-DD)."""
    d = j.kv_get(KEY) or seed(j)
    if not d:
        return None
    if evening and d.get("day") == today:        # повторный запуск вечернего отчёта в те же сутки: сравниваем с предыдущим вечером
        return d.get("prev")
    return d.get("equity")


def commit(j, equity: float, today: str) -> None:
    """Вызывать после вечернего отчёта: сегодняшний вечер становится базой следующих суток."""
    d = j.kv_get(KEY) or seed(j)
    if d and d.get("day") == today:
        return
    j.kv_set(KEY, {"day": today, "equity": float(equity), "prev": d["equity"] if d else None})
