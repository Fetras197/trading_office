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


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    """Тесты не ходят в настоящий Telegram (ни отправка, ни getUpdates): токен берётся из TG_TOKEN/конфига, на этой машине конфиг есть."""
    from office import telegram
    monkeypatch.setattr(telegram, "CONF_PATHS", [])
    monkeypatch.delenv("TG_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT", raising=False)


@pytest.fixture(autouse=True)
def _no_real_llm_keys(monkeypatch):
    """Тесты не должны доходить до настоящих моделей (в Actions ключи есть в окружении: это и расход, и сбой тестов «лимит пробрасывается»)."""
    for k in ("OPENROUTER_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
