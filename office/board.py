"""Шина board.db: отчёт каждой роли {id, role, asset, ts, вывод, уверенность 0-1, ссылки_на, данные}.
Правило ссылок (в коде): роль после аналитиков обязана сослаться на существующие отчёты предыдущих ролей этого же
цикла/актива и явно согласиться/оспорить КАЖДУЮ ссылку; без этого отчёт отклоняется (RejectedReport)."""
import datetime as dt
import json
import sqlite3
import threading

ANALYSTS = ("market", "news", "fundamentals", "quant", "buffett", "munger", "druckenmiller", "lab", "smartmoney", "decision")
# Исследовательский отдел (v3): глобальные отчёты (asset='*' или тикер), ссылок на других не требуют; зал цитирует их из любого цикла
RESEARCH = ("macro", "calendar", "newsdesk", "cryptodesk", "sectors", "portfolio_an", "picture", "regime", "postmortem", "factcheck")
STANCES = ("agree", "dispute")
PICTURE_MAX_AGE_H = 6.0   # «Картина дня» старше — не считается актуальной, ссылка на неё отклоняется

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, cycle_id TEXT, role TEXT, asset TEXT, ts TEXT, verdict TEXT,
  confidence REAL, refs TEXT, stances TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS rejected(id INTEGER PRIMARY KEY, cycle_id TEXT, role TEXT, asset TEXT, ts TEXT, reason TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS outcomes(report_id INTEGER PRIMARY KEY, label INT, correct INT);
CREATE INDEX IF NOT EXISTS ix_rep_cycle ON reports(cycle_id, asset);
CREATE INDEX IF NOT EXISTS ix_rep_role ON reports(role, id);
CREATE TABLE IF NOT EXISTS events(key TEXT PRIMARY KEY, ts TEXT, until TEXT, title TEXT, ccy TEXT, impact TEXT, kind TEXT, assets TEXT, src TEXT, data TEXT, seen TEXT);
CREATE TABLE IF NOT EXISTS memory(id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, asset TEXT, tags TEXT, text TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_mem ON memory(kind, asset, id);
CREATE TABLE IF NOT EXISTS lab_trial(key TEXT PRIMARY KEY, symbol TEXT, name TEXT, tf TEXT, params TEXT, start TEXT, status TEXT, live_ret REAL, bh_ret REAL,
  live_trades INT, live_dd REAL, bh_dd REAL, updated TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS shadow_pos(name TEXT, asset TEXT, qty REAL, entry REAL, opened TEXT, PRIMARY KEY(name, asset));
CREATE TABLE IF NOT EXISTS shadow_eq(ts TEXT, name TEXT, equity REAL, cash REAL);
CREATE INDEX IF NOT EXISTS ix_sheq ON shadow_eq(name, ts);
CREATE TABLE IF NOT EXISTS role_weights(role TEXT PRIMARY KEY, weight REAL, disabled INT DEFAULT 0, since TEXT);
CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, payload TEXT, text TEXT, status TEXT DEFAULT 'pending', decided TEXT);
CREATE TABLE IF NOT EXISTS candidates(ts TEXT, symbol TEXT, score REAL, rank INT, data TEXT);
CREATE TABLE IF NOT EXISTS strategies(id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, name TEXT, params TEXT, approved INT,
  oos_sharpe REAL, bh_sharpe REAL, oos_dd REAL, bh_dd REAL, trades INT, data TEXT);
"""


class RejectedReport(Exception):
    pass


class Board:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)
            for col in ("tf TEXT", "kind TEXT", "p REAL", "mt INT"):  # миграция: таймфрейм, вид актива, p-значение OOS и итог поправки на множественное тестирование
                try:
                    self.db.execute(f"ALTER TABLE strategies ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass
            self.db.execute("UPDATE strategies SET kind=CASE WHEN symbol LIKE '%/%' THEN 'crypto' ELSE 'stock' END WHERE kind IS NULL")
            self.db.execute("UPDATE strategies SET tf='1h' WHERE tf IS NULL")
            self.db.commit()

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def get(self, rid: int) -> dict | None:
        r = self.q("SELECT * FROM reports WHERE id=?", (rid,))
        return _row(r[0]) if r else None

    def latest(self, role: str, asset: str = "*", max_age_h: float | None = None) -> dict | None:
        """Последний отчёт роли по активу ('*' — глобальный). max_age_h — отбросить устаревший."""
        r = self.q("SELECT * FROM reports WHERE role=? AND asset=? ORDER BY id DESC LIMIT 1", (role, asset))
        if not r:
            return None
        rep = _row(r[0])
        if max_age_h is not None and age_h(rep["ts"]) > max_age_h:
            return None
        return rep

    def global_reports(self) -> list[dict]:
        """Последний отчёт каждой исследовательской роли по каждому активу — доступны для ссылок из любого цикла."""
        marks = ",".join("?" * len(RESEARCH))
        rows = self.q(f"SELECT * FROM reports WHERE id IN (SELECT MAX(id) FROM reports WHERE role IN ({marks}) GROUP BY role, asset)", RESEARCH)
        return [_row(r) for r in rows]

    def cycle_reports(self, cycle_id: str, asset: str) -> list[dict]:
        return [_row(r) for r in self.q("SELECT * FROM reports WHERE cycle_id=? AND asset=? ORDER BY id", (cycle_id, asset))]

    def publish(self, cycle_id, role, asset, verdict, confidence, refs=None, stances=None, data=None, must_cite=()) -> int:
        """must_cite — роли, отчёт хотя бы одной из которых (или каждой — см. hall) обязателен среди ссылок; проверяем каждую."""
        refs, stances = list(refs or []), dict(stances or {})
        ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        try:
            self._validate(cycle_id, role, asset, verdict, confidence, refs, stances, must_cite)
        except RejectedReport as e:
            self.x("INSERT INTO rejected(cycle_id,role,asset,ts,reason,payload) VALUES(?,?,?,?,?,?)",
                   (cycle_id, role, asset, ts, str(e), json.dumps({"verdict": verdict, "refs": refs, "stances": stances}, default=str)[:2000]))
            raise
        return self.x("INSERT INTO reports(cycle_id,role,asset,ts,verdict,confidence,refs,stances,data) VALUES(?,?,?,?,?,?,?,?,?)",
                      (cycle_id, role, asset, ts, str(verdict)[:2000], float(confidence), json.dumps(refs),
                       json.dumps({str(k): v for k, v in stances.items()}), json.dumps(data or {}, default=str, ensure_ascii=False))).lastrowid

    def _validate(self, cycle_id, role, asset, verdict, confidence, refs, stances, must_cite):
        if not str(verdict or "").strip():
            raise RejectedReport(f"{role}: пустой вывод")
        try:
            ok = 0.0 <= float(confidence) <= 1.0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise RejectedReport(f"{role}: уверенность вне 0..1")
        if role in ANALYSTS or role in RESEARCH:
            return
        if not refs:
            raise RejectedReport(f"{role}: нет ссылок на отчёты предыдущих ролей")
        known = {r["id"]: r for r in self.global_reports()}
        known.update({r["id"]: r for r in self.cycle_reports(cycle_id, asset)})
        for rid in refs:
            if rid not in known:
                raise RejectedReport(f"{role}: ссылка на несуществующий/чужой отчёт {rid}")
            if known[rid]["role"] == "picture" and age_h(known[rid]["ts"]) > PICTURE_MAX_AGE_H:
                raise RejectedReport(f"{role}: «Картина дня» #{rid} устарела (> {PICTURE_MAX_AGE_H:g} ч) — нужна актуальная")
            if str(rid) not in stances and rid not in stances:
                raise RejectedReport(f"{role}: нет явной позиции (agree/dispute) по отчёту {rid}")
        for k, v in stances.items():
            if v not in STANCES:
                raise RejectedReport(f"{role}: позиция {v!r} по {k} не agree/dispute")
        cited = {known[r]["role"] for r in refs}
        for need in must_cite:
            group = ANALYSTS if need == "analyst" else (need,)
            if not cited & set(group):
                raise RejectedReport(f"{role}: обязательна ссылка на отчёт роли {need}")


def age_h(ts: str) -> float:
    t = dt.datetime.fromisoformat(ts)
    t = t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600


def _row(r: dict) -> dict:
    return {**r, "refs": json.loads(r["refs"] or "[]"), "stances": json.loads(r["stances"] or "{}"), "data": json.loads(r["data"] or "{}")}
