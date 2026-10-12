"""Прогон исследовательского отдела вживую на ВРЕМЕННОЙ базе (боевая board.db не затрагивается): python scripts/research_dry.py [macro events news crypto sectors portfolio picture]"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from office import config, research  # noqa: E402
from office.alpaca import AlpacaClient  # noqa: E402
from office.board import Board  # noqa: E402
from office.journal import Journal  # noqa: E402
from office.llm import RoleRouter  # noqa: E402

config.load_env()
b, j = Board(), Journal()
r = research.Research(b, j, AlpacaClient(), RoleRouter(), lambda: {"held": ["ONDO/USD"], "candidates": ["SOL/USD", "DOGE/USD"]}, notify=lambda t: print("TG>>", t))
jobs = sys.argv[1:] or ["events", "macro", "news", "crypto", "sectors", "portfolio", "picture"]
for name in jobs:
    t = time.time()
    try:
        rid = getattr(r, f"run_{name}")()
        rep = b.get(rid)
        print(f"== {name} OK #{rid} {time.time() - t:.0f}s conf={rep['confidence']}\n{rep['verdict'][:500]}")
        if name in ("picture",):
            print(json.dumps(rep["data"], ensure_ascii=False, indent=1)[:1800])
    except Exception as e:  # noqa: BLE001
        print(f"== {name} FAIL {type(e).__name__}: {str(e)[:300]}")
