"""Archive windows and the mean-versus-extreme distinction."""

import httpx
import pytest
import respx

from tests.fixtures import archive_year
from weather_mcp import climate, openmeteo
from weather_mcp.geocoding import Location
from weather_mcp.openmeteo import WeatherError

URL = "https://archive-api.open-meteo.com/v1/archive"

BOSTON = Location(
    name="Boston", latitude=42.3584, longitude=-71.0598, timezone="America/New_York"
)


@pytest.fixture(autouse=True)
async def close_client():
    yield
    await openmeteo.aclose()


@pytest.fixture(autouse=True)
def fixed_year(monkeypatch):
    """Pin "now", or the expected year range moves every January."""

    class FixedDate(climate.date):
        @classmethod
        def today(cls):
            return climate.date(2026, 9, 7)

    monkeypatch.setattr(climate, "date", FixedDate)


def test_windows_uses_complete_past_years_only():
    """The archive trails real time; a partial current year would skew averages."""
    windows = climate._windows("2026-10-14", "2026-10-18", years=10)

    assert [year for year, _, _ in windows] == list(range(2016, 2026))
    assert 2026 not in [year for year, _, _ in windows]


def test_windows_keeps_the_requested_span():
    windows = climate._windows("2026-10-14", "2026-10-18", years=3)

    year, begin, finish = windows[0]
    assert (begin.month, begin.day) == (10, 14)
    assert (finish.month, finish.day) == (10, 18)


def test_windows_crossing_new_year_stay_contiguous():
    windows = climate._windows("2026-12-29", "2027-01-02", years=2)

    _, begin, finish = windows[-1]
    assert begin.isoformat() == "2025-12-29"
    assert finish.isoformat() == "2026-01-02"


def test_leap_day_is_skipped_not_slid_to_the_28th():
    """Sliding would double-count 28 February in three years out of four."""
    windows = climate._windows("2024-02-29", "2024-02-29", years=5)

    assert [year for year, _, _ in windows] == [2024]


def test_a_season_is_not_a_trip():
    with pytest.raises(WeatherError, match="at most 31 days"):
        climate._windows("2026-06-01", "2026-08-31", years=5)


def test_years_are_bounded():
    with pytest.raises(WeatherError, match="at most 30"):
        climate._windows("2026-10-14", "2026-10-18", years=99)


@respx.mock
async def test_average_high_is_the_typical_day_not_the_yearly_peak():
    """Averaging each year's *peak* reported an "average high" several degrees
    above any typical day -- exactly the number somebody packing would misread."""
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, json=archive_year([60.0, 70.0, 80.0], [40.0, 45.0, 50.0], [0.0, 0.0, 0.0])
        )
    )

    summary = await climate.typical(BOSTON, "2026-10-14", "2026-10-16", years=3)

    assert summary["averageHigh"] == 70.0   # mean of 60/70/80
    assert summary["warmest"] == 80.0       # the peak actually recorded
    assert summary["averageLow"] == 45.0
    assert summary["coldest"] == 40.0


@respx.mock
async def test_a_trace_is_not_a_rainy_day():
    """0.005in is dew. Counting it would report rain in every year, everywhere."""
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, json=archive_year([60.0, 60.0], [40.0, 40.0], [0.005, 0.0])
        )
    )

    summary = await climate.typical(BOSTON, "2026-10-14", "2026-10-15", years=2)

    assert summary["yearsWithRain"] == 0


@respx.mock
async def test_rain_above_the_threshold_counts():
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, json=archive_year([60.0, 60.0], [40.0, 40.0], [0.4, 0.0])
        )
    )

    summary = await climate.typical(BOSTON, "2026-10-14", "2026-10-15", years=2)

    assert summary["yearsWithRain"] == 2
    assert summary["byYear"][0]["wetDays"] == 1


@respx.mock
async def test_the_result_is_labeled_history():
    """It must never be formatted, or read, as a forecast."""
    respx.get(URL).mock(
        return_value=httpx.Response(200, json=archive_year([60.0], [40.0], [0.0]))
    )

    summary = await climate.typical(BOSTON, "2026-10-14", "2026-10-14", years=2)

    assert summary["kind"] == "history"
    assert summary["units"]["temperature"] == "°F"


@respx.mock
async def test_nulls_from_a_gap_do_not_become_zeroes():
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, json=archive_year([60.0, None], [40.0, None], [0.0, None])
        )
    )

    summary = await climate.typical(BOSTON, "2026-10-14", "2026-10-15", years=2)

    assert summary["averageHigh"] == 60.0
