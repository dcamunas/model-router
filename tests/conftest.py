import pytest


@pytest.fixture(autouse=True)
def _own_profile(tmp_path_factory, monkeypatch):
    """Keep every test away from the machine's Claude profile and router state: a fresh profile per test."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("profile")))


@pytest.fixture
def state():
    """Return the router's state folder in the test's profile (`<profile>/model-router`), created."""
    from model_router.paths import home

    home().mkdir(parents=True, exist_ok=True)
    return home()


@pytest.fixture
def freeze_clock(monkeypatch):
    """Return a function that sets the learning code's clock (`model_router.clock.now`) to a fixed moment."""
    from model_router import clock

    def freeze(moment):
        monkeypatch.setattr(clock, "now", lambda: moment)

    return freeze
