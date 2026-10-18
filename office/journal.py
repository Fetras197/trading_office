"""SQLite-журнал: прогоны, мнения каждой роли, решения, сделки, эквити, исходы."""
import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, ts TEXT, asset TEXT, price REAL, atr REAL, mode TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS opinions(id INTEGER PRIMARY KEY, run_id INT, ts TEXT, asset TEXT, role TEXT, stance TEXT,
  confidence REAL, rationale TEXT, raw TEXT, shadow INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, run_id INT, ts TEXT, asset TEXT, action TEXT, entry REAL,
  stop REAL, take REAL, qty REAL, verdict TEXT, reason TEXT, approved INT, executed INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY, ts TEXT, asset TEXT, side TEXT, qty REAL, price REAL, fee REAL,
  reason TEXT, run_id INT);
CREATE TABLE IF NOT EXISTS positions(asset TEXT PRIMARY KEY, qty REAL, avg REAL, stop REAL, take REAL, fees REAL,
  run_id INT, opened_ts TEXT);
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, asset TEXT, entry_ts TEXT, exit_ts TEXT, entry REAL, exit REAL,
  qty REAL, fees REAL, pnl REAL, r_mult REAL, run_id INT, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS equity(ts TEXT, equity REAL);
CREATE TABLE IF NOT EXISTS outcomes(opinion_id INTEGER PRIMARY KEY, label INT, correct INT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS nogo(id INTEGER PRIMARY KEY, ts TEXT, asset TEXT, stage TEXT, reason TEXT, score REAL);
CREATE INDEX IF NOT EXISTS ix_nogo_ts ON nogo(ts);
CREATE TABLE IF NOT EXISTS tech_signals(id INTEGER PRIMARY KEY, ts TEXT, asset TEXT, kind TEXT, price REAL, atr REAL, stop REAL, take REAL);
"""


class Journal:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def _x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def kv_get(self, k, default=None):
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def kv_set(self, k, v):
        self._x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    def add_run(self, ts, asset, price, atr, mode):
        return self._x("INSERT INTO runs(ts,asset,price,atr,mode) VALUES(?,?,?,?,?)", (ts, asset, price, atr, mode)).lastrowid

    def set_summary(self, run_id, summary):
        self._x("UPDATE runs SET summary=? WHERE id=?", (summary, run_id))

    def add_opinion(self, run_id, ts, asset, role, stance, confidence, rationale, raw=None, shadow=0):
        return self._x(
            "INSERT INTO opinions(run_id,ts,asset,role,stance,confidence,rationale,raw,shadow) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, ts, asset, role, stance, confidence, rationale, json.dumps(raw) if raw is not None else None, shadow),
        ).lastrowid

    def add_decision(self, run_id, ts, asset, action, entry, stop, take, qty, verdict, reason, approved=None):
        return self._x(
            "INSERT INTO decisions(run_id,ts,asset,action,entry,stop,take,qty,verdict,reason,approved) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, ts, asset, action, entry, stop, take, qty, verdict, reason, approved)).lastrowid

    def update_decision(self, did, **kw):
        sets = ",".join(f"{k}=?" for k in kw)
        self._x(f"UPDATE decisions SET {sets} WHERE id=?", (*kw.values(), did))

    def add_fill(self, ts, asset, side, qty, price, fee, reason, run_id):
        self._x("INSERT INTO fills(ts,asset,side,qty,price,fee,reason,run_id) VALUES(?,?,?,?,?,?,?,?)",
                (ts, asset, side, qty, price, fee, reason, run_id))

    def positions(self):
        return {r["asset"]: r for r in self.q("SELECT * FROM positions")}

    def entries_on(self, day: str) -> int:
        return self.q("SELECT COUNT(*) n FROM fills WHERE side='buy' AND substr(ts,1,10)=?", (day,))[0]["n"]

    def snap_equity(self, ts, equity):
        self._x("INSERT INTO equity(ts,equity) VALUES(?,?)", (ts, equity))

    def add_nogo(self, ts, asset, stage, reason, score):
        """Кандидат не вошёл: на какой стадии и почему; score 0..1 — близость ко входу (для отчёта «Почему не вошли»)."""
        self._x("INSERT INTO nogo(ts,asset,stage,reason,score) VALUES(?,?,?,?,?)", (ts, asset, stage, reason, float(score)))

    def nogo_top(self, since_ts: str, n: int = 3) -> list[dict]:
        """Ближайшие к входу кандидаты с момента since_ts: по одной (лучшей) записи на актив. Купленные за этот период исключаются."""
        bought = {r["asset"] for r in self.q("SELECT DISTINCT asset FROM fills WHERE side='buy' AND ts>=?", (since_ts,))}
        rows = self.q("SELECT asset, stage, reason, score, ts FROM nogo WHERE ts>=? ORDER BY score DESC, id DESC", (since_ts,))
        out, seen = [], set(bought)
        for r in rows:
            if r["asset"] not in seen:
                seen.add(r["asset"])
                out.append(r)
        return out[:n]

    def add_signal(self, ts, asset, kind, price, atr, stop, take):
        """Технический сигнал на покупку (до любых блокировок/голосов) — вход для теневого портфеля «только технический сигнал»."""
        self._x("INSERT INTO tech_signals(ts,asset,kind,price,atr,stop,take) VALUES(?,?,?,?,?,?,?)", (ts, asset, kind, price, atr, stop, take))
