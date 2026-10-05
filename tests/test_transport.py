"""Transport selection and the health endpoint.

Nothing here starts a server; `mcp.run` is replaced so that main() can be
observed deciding what to do.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from tests.fixtures import FORECAST_DAILY
from weather_mcp import openmeteo, server

FORECAST = "https://api.open-meteo.com/v1/forecast"


@pytest.fixture(autouse=True)
async def clean(monkeypatch):
    monkeypatch.setattr(openmeteo, "BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(openmeteo, "RATE_LIMIT_BACKOFF_SECONDS", 0.0)
    server._health_cache = None
    yield
    server._health_cache = None
    await openmeteo.aclose()


def test_stdio_is_the_default():
    with patch.object(server.mcp, "run") as run:
        server.main()

    run.assert_called_once_with()


def test_http_binds_where_it_is_told(monkeypatch):
    monkeypatch.setenv("WEATHER_MCP_TRANSPORT", "http")
    monkeypatch.setenv("WEATHER_MCP_PORT", "9999")

    with patch.object(server.mcp, "run") as run:
        server.main()

    assert run.call_args.kwargs["host"] == "127.0.0.1"
    assert run.call_args.kwargs["port"] == 9999


def test_http_is_stateless_by_default(monkeypatch):
    """A remote client dials from a pool of addresses; a session wedges."""
    monkeypatch.setenv("WEATHER_MCP_TRANSPORT", "http")

    with patch.object(server.mcp, "run") as run:
        server.main()

    assert run.call_args.kwargs["stateless_http"] is True


def test_sessions_can_be_restored(monkeypatch):
    monkeypatch.setenv("WEATHER_MCP_TRANSPORT", "http")
    monkeypatch.setenv("WEATHER_MCP_STATELESS", "false")

    with patch.object(server.mcp, "run") as run:
        server.main()

    assert run.call_args.kwargs["stateless_http"] is False


def test_an_open_port_without_auth_warns(monkeypatch, caplog):
    """Not fatal -- a tunnel may authenticate -- but it hands out the call budget."""
    monkeypatch.setenv("WEATHER_MCP_TRANSPORT", "http")
    monkeypatch.setenv("WEATHER_MCP_HOST", "0.0.0.0")

    with patch.object(server.mcp, "run"):
        server.main()

    assert "call budget" in caplog.text


def test_localhost_without_auth_is_silent(monkeypatch, caplog):
    monkeypatch.setenv("WEATHER_MCP_TRANSPORT", "http")

    with patch.object(server.mcp, "run"):
        server.main()

    assert "call budget" not in caplog.text


def test_loopback_recognizes_the_usual_spellings():
    assert server._is_loopback("127.0.0.1")
    assert server._is_loopback("::1")
    assert server._is_loopback("localhost")
    assert not server._is_loopback("0.0.0.0")
    assert not server._is_loopback("192.168.1.20")


@respx.mock
async def test_health_reports_ok_when_upstream_answers(home):
    respx.get(FORECAST).mock(return_value=httpx.Response(200, json=FORECAST_DAILY))

    response = await server.health(MagicMock())
    import json

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["openmeteo_reachable"] is True


@respx.mock
async def test_health_fails_without_a_home_location(monkeypatch):
    """Named places still work, but every question about "here" silently breaks.
    The monitor only reads the status code, so this has to be a 503."""
    monkeypatch.delenv("WEATHER_MCP_HOME", raising=False)
    respx.get(FORECAST).mock(return_value=httpx.Response(200, json=FORECAST_DAILY))
    import json

    response = await server.health(MagicMock())

    assert response.status_code == 503
    assert json.loads(response.body)["status"] == "no home location configured"


@respx.mock
async def test_health_publishes_whether_home_is_set_not_where_it_is(home):
    """The endpoint is effectively public; the operator's location is not."""
    respx.get(FORECAST).mock(return_value=httpx.Response(200, json=FORECAST_DAILY))
    import json

    body = json.loads((await server.health(MagicMock())).body)

    assert body["home_configured"] is True
    assert "30.2672" not in json.dumps(body)


@respx.mock
async def test_health_fails_without_leaking_the_message(home):
    """A DavError-style message can carry a URL; the class name cannot."""
    respx.get(FORECAST).mock(
        return_value=httpx.Response(200, json={"error": True, "reason": "secret detail"})
    )
    import json

    response = await server.health(MagicMock())
    body = json.loads(response.body)

    assert response.status_code == 503
    assert body["status"] == "Open-Meteo unreachable (WeatherError)"
    assert body["error"] == "WeatherError"
    assert "secret detail" not in json.dumps(body)


@respx.mock
async def test_health_lists_every_problem(monkeypatch):
    monkeypatch.delenv("WEATHER_MCP_HOME", raising=False)
    respx.get(FORECAST).mock(
        return_value=httpx.Response(200, json={"error": True, "reason": "down"})
    )
    import json

    response = await server.health(MagicMock())

    assert response.status_code == 503
    assert json.loads(response.body)["status"] == (
        "Open-Meteo unreachable (WeatherError), no home location configured"
    )


@respx.mock
async def test_health_is_cached_so_it_cannot_be_used_to_burn_the_budget():
    """Unauthenticated and public: a poll loop must not cost one call per hit."""
    route = respx.get(FORECAST).mock(return_value=httpx.Response(200, json=FORECAST_DAILY))

    for _ in range(5):
        await server.health(MagicMock())

    assert route.call_count == 1
