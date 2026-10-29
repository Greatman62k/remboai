import pytest
from app import hardening
from app.main import _OPS_FAILURES

@pytest.fixture(autouse=True)
def clear_hardening_state():
    hardening._INGRESS_LOCAL.clear()
    _OPS_FAILURES.clear()
    yield
    hardening._INGRESS_LOCAL.clear()
    _OPS_FAILURES.clear()
