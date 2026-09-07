"""Request building, the column-to-row transposition, and unit labeling."""

import httpx
import pytest
import respx

from tests.fixtures import FORECAST_DAILY, FORECAST_HOURLY
from weather_mcp import forecast, openmeteo
from weather_mcp.geocoding import Location
from weather_mcp.openmeteo import WeatherError

URL = "https://api.open-meteo.com/v1/forecast"

BOSTON = Location(
    name="Boston", latitude=42.3584, longitude=-71.0598, timezone="America/New_York"
)


@pytest.fixture(autouse=True)
async def close_client():
    yield
    await openmeteo.aclose()


def test_rows_transposes_parallel_arrays():
    rows = forecast._rows({"time": ["a", "b"], "high": [1, 2], "low": [3, 4]})

    assert rows == [
        {"time": "a", "high": 1, "low": 3},
        {"time": "b", "high": 2, "low": 4},
    ]


def test_rows_stops_at_the_shortest_column():
    """A ragged payload should shorten the answer, not raise inside formatting."""
    rows = forecast._rows({"time": ["a", "b", "c"], "high": [1, 2]})

    assert len(rows) == 2


def test_rows_of_nothing_is_empty_not_an_error():
    assert forecast._rows(None) == []
    assert forecast._rows({}) == []


def test_window_defaults_to_a_day_count_not_computed_dates():
    """The API anchors a count to the destination's today; our clock is elsewhere."""
    assert forecast._window(None, None, None) == {"forecast_days": 7}
    assert forecast._window(None, None, 3) == {"forecast_days": 3}


def test_window_takes_explicit_dates():
    assert forecast._window("2026-10-14", "2026-10-18", None) == {
        "start_date": "2026-10-14",
        "end_date": "2026-10-18",
    }


def test_one_date_alone_is_refused():
    with pytest.raises(WeatherError, match="both startDate and endDate"):
        forecast._window("2026-10-14", None, None)


def test_backwards_range_is_refused():
    with pytest.raises(WeatherError, match="before startDate"):
        forecast._window("2026-10-18", "2026-10-14", None)


def test_non_iso_dates_say_what_the_format_is():
    with pytest.raises(WeatherError, match="ISO 8601"):
        forecast._window("October 14th", "October 18th", None)


def test_past_the_horizon_points_at_typical_weather():
    """The one error a model must be able to recover from without help."""
    with pytest.raises(WeatherError, match="typical weather"):
        forecast._window(None, None, 40)


def test_units_default_american():
    assert forecast.units() == {
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
    }


def test_units_are_configurable(monkeypatch):
    monkeypatch.setenv("WEATHER_MCP_TEMPERATURE_UNIT", "celsius")
    monkeypatch.setenv("WEATHER_MCP_PRECIPITATION_UNIT", "mm")

    assert forecast.units()["temperature_unit"] == "celsius"
    assert forecast.units()["precipitation_unit"] == "mm"


def test_units_are_read_off_an_hourly_response_not_just_the_daily_block():
    """An hourly request also asks for sunrise, so `daily_units` exists and is
    useless. Reading only that left every temperature printed bare."""
    reported = forecast._units_of(FORECAST_HOURLY)

    assert reported["temperature"] == "°F"
    assert reported["precipitation"] == "inch"


@respx.mock
async def test_daily_names_conditions_rather_than_a_code():
    respx.get(URL).mock(return_value=httpx.Response(200, json=FORECAST_DAILY))

    report = await forecast.daily(BOSTON, days=3)

    assert [day["conditions"] for day in report["days"]] == [
        "clear sky",
        "overcast",
        "light rain",
    ]
    assert [day["wet"] for day in report["days"]] == [False, False, True]


@respx.mock
async def test_daily_carries_current_conditions():
    respx.get(URL).mock(return_value=httpx.Response(200, json=FORECAST_DAILY))

    report = await forecast.daily(BOSTON, days=3)

    assert report["current"]["temperature"] == 81.2
    assert report["current"]["conditions"] == "clear sky"


@respx.mock
async def test_current_can_be_left_out():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=FORECAST_DAILY))

    await forecast.daily(BOSTON, days=1, include_current=False)

    assert "current" not in route.calls[0].request.url.params


@respx.mock
async def test_every_request_asks_for_the_destinations_timezone():
    """The whole point: a forecast for Austin asked from Amsterdam."""
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=FORECAST_DAILY))

    await forecast.daily(BOSTON, days=1)

    assert route.calls[0].request.url.params["timezone"] == "auto"


@respx.mock
async def test_hourly_reports_sun_times_and_labeled_readings():
    respx.get(URL).mock(return_value=httpx.Response(200, json=FORECAST_HOURLY))

    report = await forecast.hourly(BOSTON, "2026-09-09")

    assert report["sunrise"] == "2026-09-09T07:05"
    assert report["units"]["temperature"] == "°F"
    assert len(report["hours"]) == 6
