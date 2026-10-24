import pytest


@pytest.fixture(autouse=True)
def _own_profile(tmp_path_factory, monkeypatch):
    """Keep every test away from the machine's Claude profile and router state: a fresh profile per test."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("profile")))


@pytest.fixture(autouse=True)
def _no_fable_probe(monkeypatch):
    """Never let a test start the status line's background Fable probe (a real `claude -p` call): record it."""
    from model_router import usage

    started: list[int] = []
    monkeypatch.setattr(usage, "_start_fable_probe", lambda: started.append(1))
    return started


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


@pytest.fixture(autouse=True)
def natives_hook(monkeypatch):
    """Keep the SessionStart hook from writing native settings in tests that are about something else; return the real
    step, for the tests about it to put back.
    """
    from model_router import hook

    real = hook._natives
    monkeypatch.setattr(hook, "_natives", lambda: [])
    return real
