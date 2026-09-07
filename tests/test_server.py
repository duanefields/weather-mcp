"""Tool behavior, with Open-Meteo mocked out.

What is being pinned is the part between the model and the API: which place got
picked and whether the reply says so, thresholds that decide what is worth
mentioning, and error messages that tell the model what to do differently.
"""

import httpx
import pytest
import respx

from tests.fixtures import (
    FORECAST_DAILY,
    FORECAST_HOURLY,
    GEOCODE_BOSTON,
    GEOCODE_EMPTY,
    archive_year,
)
from weather_mcp import openmeteo, server

FORECAST = "https://api.open-meteo.com/v1/forecast"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


@pytest.fixture(autouse=True)
async def close_client(monkeypatch):
    # Without this the rate-limit test really sleeps through its retries and the
    # suite takes six seconds to prove one thing.
    monkeypatch.setattr(openmeteo, "BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(openmeteo, "RATE_LIMIT_BACKOFF_SECONDS", 0.0)
    yield
    await openmeteo.aclose()


def text_of(result):
    content = result.content
    if isinstance(content, list):
        return "\n".join(getattr(block, "text", str(block)) for block in content)
    return content


@respx.mock
async def test_forecast_names_the_place_it_resolved_to():
    """The one thing about a weather reply that can be silently wrong."""
    respx.get(GEOCODE).mock(return_value=httpx.Response(200, json=GEOCODE_BOSTON))
    respx.get(FORECAST).mock(return_value=httpx.Response(200, json=FORECAST_DAILY))

    result = await server.get_forecast(location="Boston")

    assert "Boston, Massachusetts, United States" in text_of(result)


@respx.mock
async def test_forecast_reports_the_other_candidates():
    respx.get(GEOCODE).mock(return_value=httpx.Response(200, json=GEOCODE_BOSTON))
    respx.get(FORECAST).mock(return_value=httpx.Response(200, json=FORECAST_DAILY))

    result = await server.get_forecast(location="Boston")

    assert "also matched" in text_of(result)
    assert result.structured_content["alternatives"][0]["region"] == "New York"


@respx.mock
async def test_forecast_flags_a_wet_day_and_stays_quiet_about_a_dry_one():
    """A 12% chance is not worth planning around; a 71% chance is."""
    respx.get(GEOCODE).mock(return_value=httpx.Response(200, json=GEOCODE_BOSTON))
    respx.get(FORECAST).mock(return_value=httpx.Response(200, json=FORECAST_DAILY))

    body = text_of(await server.get_forecast(location="Boston"))

    assert "Rain worth planning around: 2026-09-09" in body
    assert "2026-09-08" not in body.split("Rain worth planning around:")[1]


@respx.mock
async def test_forecast_uses_home_with_no_arguments(home):
    route = respx.get(FORECAST).mock(return_value=httpx.Response(200, json=FORECAST_DAILY))

    result = await server.get_forecast()

    assert "Austin" in text_of(result)
    assert route.calls[0].request.url.params["latitude"] == "30.2672"


@respx.mock
async def test_unknown_place_is_an_error_the_model_can_act_on():
    respx.get(GEOCODE).mock(return_value=httpx.Response(200, json=GEOCODE_EMPTY))

    result = await server.get_forecast(location="Zzzyzx Nonesuch")

    assert "error" in result.structured_content
    assert "region or country" in text_of(result)


async def test_past_the_horizon_points_at_the_other_tool(home):
    """No HTTP mock: this must fail before any request goes out."""
    result = await server.get_forecast(days=40)

    assert "typical weather" in text_of(result)


@respx.mock
async def test_hourly_groups_rain_into_a_window():
    """The actual question behind an hourly lookup, rather than 24 rows to scan."""
    respx.get(GEOCODE).mock(return_value=httpx.Response(200, json=GEOCODE_BOSTON))
    respx.get(FORECAST).mock(return_value=httpx.Response(200, json=FORECAST_HOURLY))

    result = await server.get_hourly_forecast(date="2026-09-09", location="Boston")

    assert result.structured_content["wetWindows"] == [{"from": "02:00", "to": "05:00"}]
    assert "Rain likely: 02:00–05:00" in text_of(result)


def test_a_single_wet_hour_is_not_a_zero_length_window():
    hours = [
        {"time": "2026-09-09T13:00", "precipitationChance": 5, "wet": False},
        {"time": "2026-09-09T14:00", "precipitationChance": 80, "wet": True},
        {"time": "2026-09-09T15:00", "precipitationChance": 5, "wet": False},
    ]

    assert server._wet_windows(hours) == [("14:00", "15:00")]


@respx.mock
async def test_hourly_labels_readings_with_their_units():
    """An hourly response's daily_units block has no temperature key, which once
    left every reading printed bare."""
    respx.get(GEOCODE).mock(return_value=httpx.Response(200, json=GEOCODE_BOSTON))
    respx.get(FORECAST).mock(return_value=httpx.Response(200, json=FORECAST_HOURLY))

    body = text_of(await server.get_hourly_forecast(date="2026-09-09", location="Boston"))

    assert "°F" in body
    assert "inch" in body


@respx.mock
async def test_typical_weather_says_it_is_history_in_the_first_lines():
    """It must never be read, or reported, as a forecast."""
    respx.get(GEOCODE).mock(return_value=httpx.Response(200, json=GEOCODE_BOSTON))
    respx.get(ARCHIVE).mock(
        return_value=httpx.Response(200, json=archive_year([60.0, 64.0], [44.0, 46.0], [0.0, 0.3]))
    )

    result = await server.get_typical_weather(
        startDate="2026-10-14", endDate="2026-10-15", location="Boston", years=3
    )

    assert "not a forecast" in text_of(result)
    assert result.structured_content["kind"] == "history"


@respx.mock
async def test_best_days_ranks_and_excludes_the_unusable(home):
    """A list padded to `limit` with days scored zero reads as five options."""
    hours = {
        "timezone": "America/Chicago",
        "hourly_units": {"temperature_2m": "°F", "precipitation": "inch", "wind_speed_10m": "mp/h"},
        "hourly": {
            "time": [f"2026-09-{day:02d}T{hour:02d}:00" for day in (18, 19) for hour in range(8, 12)],
            "temperature_2m": [65.0] * 4 + [100.0] * 4,
            "apparent_temperature": [65.0] * 4 + [105.0] * 4,
            "precipitation_probability": [0] * 8,
            "precipitation": [0.0] * 8,
            "weather_code": [0] * 8,
            "wind_speed_10m": [5.0] * 8,
            "relative_humidity_2m": [50] * 8,
        },
    }
    respx.get(FORECAST).mock(return_value=httpx.Response(200, json=hours))

    result = await server.find_best_days(days=2)
    body = text_of(result)

    assert "1. 2026-09-18" in body
    assert "2. " not in body
    assert "Worth avoiding: 2026-09-19" in body
    # Both days are still in the structured payload; only the prose is filtered.
    assert len(result.structured_content["days"]) == 2


@respx.mock
async def test_best_days_says_so_when_nothing_is_any_good(home):
    hours = {
        "timezone": "America/Chicago",
        "hourly_units": {"temperature_2m": "°F"},
        "hourly": {
            "time": [f"2026-09-18T{hour:02d}:00" for hour in range(8, 12)],
            "temperature_2m": [100.0] * 4,
            "apparent_temperature": [108.0] * 4,
            "precipitation_probability": [0] * 4,
            "precipitation": [0.0] * 4,
            "weather_code": [0] * 4,
            "wind_speed_10m": [5.0] * 4,
            "relative_humidity_2m": [50] * 4,
        },
    }
    respx.get(FORECAST).mock(return_value=httpx.Response(200, json=hours))

    assert "scores better than poor" in text_of(await server.find_best_days(days=1))


async def test_best_days_rejects_a_backwards_window(home):
    result = await server.find_best_days(startHour=18, endHour=8)

    assert "must be after" in text_of(result)


@respx.mock
async def test_search_locations_lists_candidates_with_coordinates():
    respx.get(GEOCODE).mock(return_value=httpx.Response(200, json=GEOCODE_BOSTON))

    result = await server.search_locations(query="Boston")

    assert len(result.structured_content["locations"]) == 2
    assert "42.3584, -71.0598" in text_of(result)


@respx.mock
async def test_a_rate_limit_reads_as_temporary_not_broken():
    respx.get(GEOCODE).mock(return_value=httpx.Response(200, json=GEOCODE_BOSTON))
    respx.get(FORECAST).mock(return_value=httpx.Response(429))

    body = text_of(await server.get_forecast(location="Boston"))

    assert "Nothing is broken" in body
