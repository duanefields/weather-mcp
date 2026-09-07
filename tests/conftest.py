"""Suite-wide fixtures.

Two pieces of process-global state have to be reset between tests, or one test's
success quietly becomes another's: the geocoding cache, which would let a test
that never mocked an HTTP call still get an answer, and the shared HTTP client,
which respx patches at the transport level.

The environment is scrubbed for the same reason. `WEATHER_MCP_HOME` is set on
the developer's own machine, so a test asserting that an unset home produces a
useful error passes on CI and fails on the laptop -- or worse, the reverse.
"""

import pytest

from weather_mcp import geocoding

WEATHER_ENV = [
    "WEATHER_MCP_HOME",
    "WEATHER_MCP_HOME_NAME",
    "WEATHER_MCP_HOME_TIMEZONE",
    "WEATHER_MCP_TEMPERATURE_UNIT",
    "WEATHER_MCP_WIND_SPEED_UNIT",
    "WEATHER_MCP_PRECIPITATION_UNIT",
    "WEATHER_MCP_TRANSPORT",
    "WEATHER_MCP_HOST",
    "WEATHER_MCP_PORT",
    "WEATHER_MCP_AUTH",
]


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    for name in WEATHER_ENV:
        monkeypatch.delenv(name, raising=False)
    geocoding.clear_cache()
    yield
    geocoding.clear_cache()


@pytest.fixture
def home(monkeypatch):
    """A configured home location, for tests about the no-argument path."""
    monkeypatch.setenv("WEATHER_MCP_HOME", "30.2672,-97.7431")
    monkeypatch.setenv("WEATHER_MCP_HOME_NAME", "Austin")
    monkeypatch.setenv("WEATHER_MCP_HOME_TIMEZONE", "America/Chicago")
