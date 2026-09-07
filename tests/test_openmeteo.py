"""The HTTP layer: parameter encoding, error mapping, retries.

Nothing here reaches the network. What is being pinned is the set of failure
shapes Open-Meteo actually produces -- an error reported with a 200, a burst
rejection that clears on retry -- because each of them costs a real debugging
session when it is not handled.
"""

import httpx
import pytest
import respx

from weather_mcp import openmeteo
from weather_mcp.openmeteo import RateLimited, WeatherError

URL = "https://api.open-meteo.com/v1/forecast"


@pytest.fixture(autouse=True)
async def fast_retries(monkeypatch):
    """Keep the retry tests from actually sleeping for seconds."""
    monkeypatch.setattr(openmeteo, "BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(openmeteo, "RATE_LIMIT_BACKOFF_SECONDS", 0.0)
    yield
    await openmeteo.aclose()


@respx.mock
async def test_drops_none_and_joins_lists():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    await openmeteo.get(URL, {"daily": ["a", "b"], "empty": None, "flag": True, "n": 3})

    query = route.calls[0].request.url.params
    assert query["daily"] == "a,b"
    assert "empty" not in query
    assert query["flag"] == "true"
    assert query["n"] == "3"


@respx.mock
async def test_empty_list_is_dropped_not_sent_blank():
    """`daily=` with no value is a different request from omitting `daily`."""
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    await openmeteo.get(URL, {"daily": [], "latitude": 1.0})

    assert "daily" not in route.calls[0].request.url.params


@respx.mock
async def test_in_band_error_becomes_an_exception():
    """Open-Meteo reports a bad parameter with a 200 as often as a 400."""
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "error": True,
                "reason": "Parameter 'start_date' is out of allowed range",
            },
        )
    )

    with pytest.raises(WeatherError, match="out of allowed range"):
        await openmeteo.get(URL, {})


@respx.mock
async def test_reason_survives_a_400():
    """The useful part of a 400 is in the body, so the body is read first."""
    respx.get(URL).mock(
        return_value=httpx.Response(400, json={"error": True, "reason": "Forecast days is invalid."})
    )

    with pytest.raises(WeatherError, match="Forecast days is invalid"):
        await openmeteo.get(URL, {})


@respx.mock
async def test_rate_limit_is_retried_and_then_succeeds():
    """A 429 is a burst guard that clears, not an exhausted daily budget.

    Ten concurrent archive requests reliably get several rejected and the same
    ones accepted a second later. Failing on the first 429 made
    get_typical_weather fail outright.
    """
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    assert await openmeteo.get(URL, {}) == {"ok": True}


@respx.mock
async def test_persistent_rate_limit_says_so_specifically():
    respx.get(URL).mock(return_value=httpx.Response(429))

    with pytest.raises(RateLimited, match="too frequent"):
        await openmeteo.get(URL, {})


@respx.mock
async def test_server_error_is_retried():
    respx.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
    )

    assert await openmeteo.get(URL, {}) == {"ok": True}


@respx.mock
async def test_client_error_is_not_retried():
    """A 404 will fail identically on a retry; spending two more calls is waste."""
    route = respx.get(URL).mock(return_value=httpx.Response(404, text="nope"))

    with pytest.raises(WeatherError):
        await openmeteo.get(URL, {})

    assert route.call_count == 1


@respx.mock
async def test_network_failure_is_retried_then_reported():
    respx.get(URL).mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(WeatherError, match="Could not reach Open-Meteo"):
        await openmeteo.get(URL, {})


@respx.mock
async def test_non_json_body_is_not_a_traceback():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))

    with pytest.raises(WeatherError, match="not JSON"):
        await openmeteo.get(URL, {})
