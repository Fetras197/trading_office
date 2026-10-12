"""Вес голоса по заслугам (2.3). Точность роли = доля верных мнений по triple-barrier (outcomes в board.db); калибровка = заявленная уверенность против реальной доли верных.
Новые роли стартуют с нейтральным весом 1,0; пока у роли меньше MIN_N оценённых мнений, вес не предлагается. Изменение веса (и отключение роли) —
только предложение (таблица proposals), в силу вступает после ответа «да» в Telegram (4.3). Зал взвешивает голоса активными весами."""
import datetime as dt
import json

MIN_N = 10            # минимум оценённых мнений роли до любого изменения веса
SHRINK = 20           # осторожность: вес тянется к 1,0 тем сильнее, чем меньше оценок
STEP = 0.15           # минимальное отличие для предложения
DISABLE_ACC, DISABLE_N = 0.35, 30
BUCKETS = ((0.0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01))


def stats(board) -> dict[str, dict]:
    rows = board.q("SELECT r.role role, r.confidence c, x.correct k FROM reports r JOIN outcomes x ON x.report_id=r.id")
    out: dict[str, dict] = {}
    for r in rows:
        s = out.setdefault(r["role"], {"n": 0, "hits": 0, "conf_sum": 0.0, "b": {b: [0, 0] for b in BUCKETS}})
        s["n"] += 1
        s["hits"] += r["k"]
        s["conf_sum"] += r["c"]
        for lo, hi in BUCKETS:
            if lo <= r["c"] < hi:
                s["b"][(lo, hi)][0] += 1
                s["b"][(lo, hi)][1] += r["k"]
    for s in out.values():
        s["acc"] = s["hits"] / s["n"]
        s["mean_conf"] = s["conf_sum"] / s["n"]
        s["overconfidence"] = round(s["mean_conf"] - s["acc"], 3)    # > 0: заявляет больше, чем сбывается
        s["calibration"] = {f"{lo:.1f}-{min(hi, 1):.1f}": (round(h / n, 2), n) for (lo, hi), (n, h) in s["b"].items() if n}
        del s["b"]
    return out


def active(board) -> dict[str, dict]:
    return {r["role"]: {"weight": r["weight"], "disabled": bool(r["disabled"])} for r in board.q("SELECT * FROM role_weights")}


def weight_of(wts: dict, role: str) -> float:
    return wts.get(role, {}).get("weight", 1.0)


def track_record(st: dict, role: str) -> str:
    s = st.get(role)
    if not s or s["n"] < MIN_N:
        return f"new role, neutral weight (only {s['n'] if s else 0} evaluated opinions)"
    return f"hit rate {s['acc']:.0%} over {s['n']} evaluated opinions; claimed confidence avg {s['mean_conf']:.2f} (overconfidence {s['overconfidence']:+.2f})"


def calibrated_conf(st: dict, role: str, conf: float) -> float:
    """Заявленная уверенность, приведённая к реальной точности роли (только если оценок ≥ MIN_N); иначе без изменений."""
    s = st.get(role)
    if not s or s["n"] < MIN_N or s["mean_conf"] <= 0:
        return conf
    return round(max(0.0, min(1.0, conf * s["acc"] / s["mean_conf"])), 3)


def weighted_vote(reports: list[dict], wts: dict) -> float:
    """Σ w·conf·знак / Σ w по направленным голосам: +1 за покупку, −1 за продажу, 0 — hold. Диапазон −1..+1."""
    num = den = 0.0
    for r in reports:
        if r["stance"] == "abstain" or wts.get(r["role"], {}).get("disabled"):
            continue
        w = weight_of(wts, r["role"])
        sign = {"buy": 1, "sell": -1}.get(r["stance"], 0)
        num += w * r["confidence"] * sign
        den += w
    return round(num / den, 3) if den else 0.0


def propose(board, now=None) -> list[int]:
    """Пересчитать предложения по весам/отключению. Возвращает id новых предложений (уже существующие pending обновляются)."""
    from .report import ROLE_RU
    ts = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    st = {r: s for r, s in stats(board).items() if s["n"] >= MIN_N}
    if not st:
        return []
    mean_acc = sum(s["acc"] * s["n"] for s in st.values()) / sum(s["n"] for s in st.values())
    act, new = active(board), []
    for role, s in st.items():
        ru = ROLE_RU.get(role, {"bull": "Бык", "bear": "Медведь", "trader": "Трейдер"}.get(role, role))
        cur = weight_of(act, role)
        target = max(0.5, min(1.5, s["acc"] / mean_acc)) if mean_acc else 1.0
        w = round(1 + (target - 1) * s["n"] / (s["n"] + SHRINK), 2)
        items = []
        if abs(w - cur) >= STEP:
            verb = "Повысить" if w > cur else "Понизить"
            items.append(("weight", {"role": role, "from": cur, "to": w, "n": s["n"], "acc": round(s["acc"], 3)},
                          f"{verb} вес роли «{ru}» с {cur:.2f} до {w:.2f}: точность {s['acc'] * 100:.0f}% на {s['n']} оценках".replace(".", ",")))
        if s["acc"] < DISABLE_ACC and s["n"] >= DISABLE_N and not act.get(role, {}).get("disabled"):
            items.append(("disable", {"role": role, "n": s["n"], "acc": round(s["acc"], 3)}, f"Отключить роль «{ru}»: точность {s['acc'] * 100:.0f}% на {s['n']} оценках".replace(".", ",")))
        for kind, payload, text in items:
            old = board.q("SELECT id, payload FROM proposals WHERE status='pending' AND kind=? AND json_extract(payload,'$.role')=?", (kind, role))
            if old:
                board.x("UPDATE proposals SET payload=?, text=?, ts=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), text, ts, old[0]["id"]))
            else:
                new.append(board.x("INSERT INTO proposals(ts,kind,payload,text) VALUES(?,?,?,?)", (ts, kind, json.dumps(payload, ensure_ascii=False), text)).lastrowid)
        if not items:   # условие исчезло — устаревшие предложения снимаем
            board.x("UPDATE proposals SET status='obsolete' WHERE status='pending' AND json_extract(payload,'$.role')=?", (role,))
    return new


def pending(board) -> list[dict]:
    return board.q("SELECT * FROM proposals WHERE status='pending' ORDER BY id")


def decide(board, pid: int, yes: bool) -> str:
    p = board.q("SELECT * FROM proposals WHERE id=? AND status='pending'", (pid,))
    if not p:
        return ""
    p = p[0]
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if yes:
        d = json.loads(p["payload"])
        if p["kind"] == "weight":
            board.x("INSERT INTO role_weights(role,weight,disabled,since) VALUES(?,?,0,?) ON CONFLICT(role) DO UPDATE SET weight=excluded.weight, since=excluded.since", (d["role"], d["to"], ts))
        elif p["kind"] == "disable":
            board.x("INSERT INTO role_weights(role,weight,disabled,since) VALUES(?,1.0,1,?) ON CONFLICT(role) DO UPDATE SET disabled=1, since=excluded.since", (d["role"], ts))
    board.x("UPDATE proposals SET status=?, decided=? WHERE id=?", ("applied" if yes else "rejected", ts, pid))
    return p["text"]
