import pytest


@pytest.fixture(autouse=True)
def _own_state_dir(tmp_path_factory, monkeypatch):
    """Keep every test away from the machine's router state: its config, calibrations and ledger are not the suite's."""
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path_factory.mktemp("state")))


@pytest.fixture
def freeze_clock(monkeypatch):
    """Return a function that sets the learning code's clock (`model_router.clock.now`) to a fixed moment."""
    from model_router import clock

    def freeze(moment):
        monkeypatch.setattr(clock, "now", lambda: moment)

    return freeze
