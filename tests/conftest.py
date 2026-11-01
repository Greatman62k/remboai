import pytest
from app import hardening
from app.ai.llm import _BREAKER
from app.main import _OPS_FAILURES
from app.agent.client import _NO_TOOLS

@pytest.fixture(autouse=True)
def clear_hardening_state():
    hardening._INGRESS_LOCAL.clear()
    _BREAKER.clear()
    _OPS_FAILURES.clear()
    _NO_TOOLS.clear()
    yield
    hardening._INGRESS_LOCAL.clear()
    _BREAKER.clear()
    _OPS_FAILURES.clear()
    _NO_TOOLS.clear()
