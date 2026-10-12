"""Расписание заданий по местному времени владельца (Europe/Minsk) с памятью о последнем запуске в journal.kv (рестарт процесса не повторяет и не теряет задания).
Job.every — интервал в секундах; Job.at — список «ЧЧ:ММ» (опц. weekday: 0=пн … 6=вс); catchup_h — насколько позже назначенного времени задание ещё имеет смысл.
Никогда запущенное at-задание выполняется сразу (первичное наполнение отчётами). Ошибка задания не валит планировщик; повтор не раньше чем через retry_s."""
import datetime as dt
import threading
import traceback
from dataclasses import dataclass, field
from typing import Callable

from . import config


@dataclass
class Job:
    name: str
    fn: Callable
    every: float | None = None
    at: list[str] = field(default_factory=list)
    weekday: int | None = None
    catchup_h: float = 3.0
    retry_s: float = 900
    bootstrap: bool = True   # at-задание, никогда не запускавшееся, выполнить сразу


class Scheduler:
    def __init__(self, kv, jobs, now_fn=None, log=print):
        self.kv, self.jobs, self.log = kv, jobs, log
        self.now_fn = now_fn or config.localnow
        self._triggers: set[str] = set()
        self._lock = threading.Lock()

    def trigger(self, name: str):
        """Внеочередной запуск при ближайшем проходе (например, портфельный аналитик после сделки)."""
        with self._lock:
            self._triggers.add(name)

    def _ts(self, k):
        v = self.kv.kv_get(k)
        return dt.datetime.fromisoformat(v) if v else None

    def is_due(self, job: Job, now: dt.datetime) -> bool:
        with self._lock:
            if job.name in self._triggers:
                return True
        last, attempt = self._ts(f"job:{job.name}:last"), self._ts(f"job:{job.name}:attempt")
        if attempt and (now - attempt).total_seconds() < min(job.retry_s, job.every or job.retry_s) and not (last and attempt <= last):
            return False   # недавняя неудачная попытка
        if job.every:
            return last is None or (now - last).total_seconds() >= job.every
        if last is None:
            if not job.bootstrap:   # не догонять прошлое: считаем, что до первого наблюдения задание «уже отработало»
                self.kv.kv_set(f"job:{job.name}:last", now.isoformat())
                last = now
            else:
                return True
        for back in (0, 1):
            day = (now - dt.timedelta(days=back)).date()
            for hhmm in job.at:
                h, m = map(int, hhmm.split(":"))
                sched = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=now.tzinfo)
                if job.weekday is not None and sched.weekday() != job.weekday:
                    continue
                if sched <= now <= sched + dt.timedelta(hours=job.catchup_h) and last < sched:
                    return True
        return False

    def run_due(self, now: dt.datetime | None = None) -> list[str]:
        now = now or self.now_fn()
        ran = []
        for job in self.jobs:
            if not self.is_due(job, now):
                continue
            with self._lock:
                self._triggers.discard(job.name)
            self.kv.kv_set(f"job:{job.name}:attempt", now.isoformat())
            try:
                job.fn()
                self.kv.kv_set(f"job:{job.name}:last", now.isoformat())
                ran.append(job.name)
            except Exception as e:  # noqa: BLE001 — упавшее задание не валит остальные
                self.log(f"{now:%F %T} задание {job.name} упало: {type(e).__name__}: {str(e)[:200]}")
                if not isinstance(e, (OSError, ValueError, KeyError)):
                    self.log(traceback.format_exc(limit=3))
        return ran
