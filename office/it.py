"""IT-отдел (5.1). Раз в 5 минут: заряд телефона (termux-battery-status, доступен и из proot), интернет, живость лаборатории (перезапуск упавшей),
давление лимитов Claude/Gemini → режим экономии (меньше активов, реже циклы). Тревоги в Telegram с «антиспамом»; пока интернета нет, тревога идёт локальным
уведомлением Termux и уходит в Telegram после возвращения связи. Упавший supervisor поднимает `office.sh watchdog` (cron в Termux); при следующем старте
офис сообщает, сколько не работал. Файл NO_LAB в корне проекта = лабораторию не перезапускать (экономия памяти)."""
import datetime as dt
import json
import os
import subprocess
import sys
import time

import requests

from . import config, llm

TERMUX = "/data/data/com.termux/files/usr/bin"
BAT_WARN, BAT_CRIT = 20, 10
ECON_ASSETS, ECON_CYCLE_MULT = 4, 2
HEARTBEAT = config.ROOT / "logs" / "heartbeat"
HEALTH = config.ROOT / "logs" / "health.json"       # пишет office.sh watchdog (запасной источник данных о батарее)


def battery() -> dict | None:
    try:
        r = subprocess.run([f"{TERMUX}/termux-battery-status"], capture_output=True, text=True, timeout=15)
        j = json.loads(r.stdout)
        return {"pct": int(j["percentage"]), "plugged": j.get("plugged", "UNPLUGGED") != "UNPLUGGED", "src": "live"}
    except Exception:  # noqa: BLE001
        pass
    try:   # запасной вариант: файл сторожа, если свежий (< 15 мин)
        if HEALTH.exists() and time.time() - HEALTH.stat().st_mtime < 900:
            j = json.loads(HEALTH.read_text())
            return {"pct": int(j["percentage"]), "plugged": j.get("plugged", "UNPLUGGED") != "UNPLUGGED", "src": "file"}
    except Exception:  # noqa: BLE001
        pass
    return None


def internet_ok() -> bool:
    for url in ("https://api.telegram.org", "https://paper-api.alpaca.markets"):
        try:
            requests.head(url, timeout=8)
            return True
        except requests.RequestException:
            continue
    return False


def local_alert(text: str) -> None:
    """Уведомление на самом телефоне (работает без интернета)."""
    try:
        subprocess.run([f"{TERMUX}/termux-notification", "--title", "Торговый офис", "--content", text], capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def lab_alive() -> bool:
    try:
        pid = int((config.ROOT / ".lab.pid").read_text().strip())
        return b"office\x00lab" in open(f"/proc/{pid}/cmdline", "rb").read()
    except Exception:  # noqa: BLE001
        return False


def lab_restart() -> None:
    (config.ROOT / "logs").mkdir(exist_ok=True)
    p = subprocess.Popen([sys.executable, "-m", "office", "lab"], cwd=config.ROOT, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=open(config.ROOT / "logs" / "lab.log", "a"), stderr=subprocess.STDOUT, preexec_fn=lambda: os.nice(15))
    (config.ROOT / ".lab.pid").write_text(str(p.pid))


class ITDept:
    def __init__(self, kv, notify, now_fn=time.time, battery_fn=battery, net_fn=internet_ok, lab_alive_fn=lab_alive, lab_restart_fn=lab_restart, local_fn=local_alert, pressure_fn=llm.pressure):
        self.kv, self.notify, self.now = kv, notify, now_fn
        self.battery, self.net, self.lab_alive, self.lab_restart, self.local, self.pressure = battery_fn, net_fn, lab_alive_fn, lab_restart_fn, local_fn, pressure_fn
        self.economy = bool(kv.kv_get("it:economy"))

    # ---- антиспам ----
    def _cool(self, key: str, seconds: float) -> bool:
        t = self.now()
        if t - (self.kv.kv_get(f"it:alert:{key}") or 0) < seconds:
            return False
        self.kv.kv_set(f"it:alert:{key}", t)
        return True

    def _hhmm(self, ts: float) -> str:
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).astimezone(config.localnow().tzinfo).strftime("%H:%M")

    # ---- проверки ----
    def check_battery(self):
        b = self.battery()
        if b is None:
            if self._cool("battery_nodata", 12 * 3600):
                self.notify("🔋 Нет данных о заряде телефона: проверьте, что установлено приложение Termux:API.")
            return
        if b["pct"] < BAT_WARN and not b["plugged"]:
            crit = b["pct"] < BAT_CRIT
            if self._cool("battery_crit" if crit else "battery_low", 3600 if crit else 3 * 3600):
                msg = f"🔋 Заряд телефона {b['pct']}% — подключите зарядку, иначе офис остановится"
                self.local(msg)
                self.notify(msg)

    def check_net(self):
        ok, t = self.net(), self.now()
        down = self.kv.kv_get("it:net_down")
        if not ok:
            if down is None:
                self.kv.kv_set("it:net_down", t)
            elif t - down >= 600 and self._cool("net_local", 3600):
                self.local("Нет интернета уже 10 минут: офис не может торговать и отправлять отчёты")
            return
        if down is not None:
            self.kv.kv_set("it:net_down", None)
            mins = int((t - down) / 60)
            if mins >= 10:
                self.notify(f"📡 Интернет пропадал с {self._hhmm(down)} до {self._hhmm(t)} ({mins} мин). Сейчас связь восстановлена, офис работает.")

    def check_lab(self):
        if (config.ROOT / "NO_LAB").exists() or (config.ROOT / ".user_stopped").exists():
            return
        if not self.lab_alive() and self._cool("lab_restart", 1800):
            try:
                self.lab_restart()
                self.notify("🔧 Лаборатория стратегий остановилась и перезапущена.")
            except Exception:  # noqa: BLE001
                self.notify("⚠️ Лаборатория стратегий остановилась, перезапустить не удалось — нужен ручной запуск.")

    def check_limits(self):
        p = self.pressure()
        hot = [n for n, v in (("Claude", p.get("claude")), ("Gemini", p.get("gemini"))) if v]
        if hot and not self.economy:
            self.economy = True
            self.kv.kv_set("it:economy", 1)
            self.kv.kv_set("it:economy_since", self.now())
            self.notify(f"🐢 Режим экономии: лимиты нейросетей ({', '.join(hot)}) почти исчерпаны — проверяю меньше активов и реже. Включится обычный режим, когда лимиты вернутся.")
        elif not hot and self.economy and self.now() - (self.kv.kv_get("it:economy_since") or 0) > 3600:
            self.economy = False
            self.kv.kv_set("it:economy", 0)
            self.notify("✅ Режим экономии выключен: лимиты нейросетей восстановились.")

    def check(self):
        for fn in (self.check_battery, self.check_net, self.check_lab, self.check_limits):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — IT-отдел сам не должен ронять офис
                print(f"{time.strftime('%F %T')} IT {fn.__name__}: {type(e).__name__}: {str(e)[:120]}", flush=True)

    # ---- сердцебиение и старт ----
    def heartbeat(self):
        try:
            HEARTBEAT.parent.mkdir(exist_ok=True)
            HEARTBEAT.write_text(str(self.now()))
        except OSError:
            pass

    def on_start(self):
        """Если процесс был недоступен дольше 10 минут (падение, перезагрузка телефона) — сообщить."""
        try:
            last = float(HEARTBEAT.read_text())
        except (OSError, ValueError):
            return
        gap = self.now() - last
        if gap > 600:
            self.notify(f"⚠️ Офис не работал {int(gap / 60)} мин (с {self._hhmm(last)} до {self._hhmm(self.now())}) и снова запущен. Позиции всё это время были под защитными стопами брокера.")
