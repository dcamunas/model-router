import pytest


@pytest.fixture(autouse=True)
def _own_state_dir(tmp_path_factory, monkeypatch):
    """Keep every test away from ~/.model-router: its config, calibrations and ledger are the machine's, not the suite's."""
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path_factory.mktemp("state")))
