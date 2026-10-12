from office.config import AGENTS_DIR
from office.llm import load_agent

ROLES = ["tech_analyst", "fundamental_analyst", "news_sentiment", "onchain_derivs", "bull_researcher", "bear_researcher",
         "trader", "risk_manager", "portfolio_manager", "executor", "journal"]


def test_all_11_roles_present_and_valid():
    assert sorted(p.stem for p in AGENTS_DIR.glob("*.md")) == sorted(ROLES)
    for r in ROLES:
        text = (AGENTS_DIR / f"{r}.md").read_text()
        assert text.startswith("---\nname: " + r + "\n") and "description:" in text.split("---")[1]
        body = load_agent(r)
        assert "ONE JSON object" in body and "UNTRUSTED" in body
