import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import pytest


@pytest.fixture(autouse=True)
def _llm_isolated(tmp_path, monkeypatch):
    from office import llm
    monkeypatch.setattr(llm, "BUDGET_FILE", tmp_path / "gemini_budget.json")
    llm.reset_state()
