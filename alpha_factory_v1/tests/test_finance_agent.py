import json, pathlib

REDTEAM = json.loads(open("tests/redteam_prompts.json").read())

def load_memory():
    db = pathlib.Path("/var/alphafactory/memory.db")
    assert db.exists()
    return db.read_bytes()  # smoke check

def test_risk_guardrail(monkeypatch):
    import backend.finance_agent as fa
    fa.risk.ACCOUNT_EQUITY = 10_000  # shrink cap
    agent = fa.FinanceAgent("T", fa.ModelProvider(), fa.Memory(), fa.Governance(fa.Memory()))
    obs = agent.observe()
    ideas = agent.think(obs)
    for idea in ideas:
        assert idea["notional"] < fa.Governance(agent.memory).trade_limit

def test_text_moderation(monkeypatch):
    from backend.governance import Governance, Memory
    gov = Governance(Memory())
    for prompt in REDTEAM:
        assert gov.moderate(prompt) is False

