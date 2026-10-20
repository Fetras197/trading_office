import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import pytest


@pytest.fixture(autouse=True)
def _llm_isolated(tmp_path, monkeypatch):
    from office import llm
    monkeypatch.setattr(llm, "BUDGET_FILE", tmp_path / "gemini_budget.json")
    llm.reset_state()


@pytest.fixture(autouse=True)
def _no_real_earnings_lookup(monkeypatch):
    """По умолчанию в тестах отчётов у компаний нет (без сети); тесты правила отчётов подменяют сами."""
    from office import earnings
    monkeypatch.setattr(earnings, "next_report", lambda symbol, board=None, today=None: (None, True))
