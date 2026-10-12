"""Память офиса (1.8): выводы прошлых дней (картины дня, макро-заметки, уроки по сделкам) хранятся в board.memory и подмешиваются в новые отчёты."""
import datetime as dt
import json


def remember(board, kind: str, text: str, asset: str = "*", tags=(), data=None) -> int:
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return board.x("INSERT INTO memory(ts,kind,asset,tags,text,data) VALUES(?,?,?,?,?,?)",
                   (ts, kind, asset, ",".join(tags), text[:1500], json.dumps(data or {}, ensure_ascii=False, default=str))).lastrowid


def recall(board, kind: str, asset: str | None = None, days: float = 7, limit: int = 8, tag: str | None = None) -> list[dict]:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")
    sql, args = "SELECT ts, asset, tags, text FROM memory WHERE kind=? AND ts>=?", [kind, since]
    if asset:
        sql += " AND asset=?"
        args.append(asset)
    if tag:
        sql += " AND (',' || tags || ',') LIKE ?"
        args.append(f"%,{tag},%")
    rows = board.q(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
    return list(reversed(rows))


def brief(board, kind: str, **kw) -> list[str]:
    """Компактный вид для промпта: «дата: текст»."""
    return [f"{r['ts'][:16]}: {r['text']}" for r in recall(board, kind, **kw)]
