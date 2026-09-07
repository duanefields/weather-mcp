"""Place resolution, the cache, and the home default."""

import httpx
import pytest
import respx

from tests.fixtures import GEOCODE_BOSTON, GEOCODE_EMPTY
from weather_mcp import geocoding, openmeteo
from weather_mcp.geocoding import Location, UnknownPlace
from weather_mcp.openmeteo import WeatherError

URL = "https://geocoding-api.open-meteo.com/v1/search"


@pytest.fixture(autouse=True)
async def close_client():
    yield
    await openmeteo.aclose()


def test_label_names_region_and_country():
    """The one thing about a weather reply that can be silently wrong."""
    location = Location(
        name="Boston", latitude=42.4, longitude=-71.1, timezone="America/New_York",
        country="United States", admin1="Massachusetts",
    )
    assert location.label == "Boston, Massachusetts, United States"


def test_label_does_not_repeat_a_city_state():
    """New York, New York reads badly and says nothing extra."""
    location = Location(
        name="New York", latitude=40.7, longitude=-74.0, timezone="America/New_York",
        country="United States", admin1="New York",
    )
    assert location.label == "New York, United States"


@respx.mock
async def test_search_returns_ranked_candidates():
    respx.get(URL).mock(return_value=httpx.Response(200, json=GEOCODE_BOSTON))

    results = await geocoding.search("Boston")

    assert [r.admin1 for r in results] == ["Massachusetts", "New York"]
    assert results[0].timezone == "America/New_York"


@respx.mock
async def test_results_are_cached():
    """Where a city is does not change, so a second lookup must not spend a call."""
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=GEOCODE_BOSTON))

    await geocoding.search("Boston")
    await geocoding.search("Boston")

    assert route.call_count == 1


@respx.mock
async def test_cache_is_case_insensitive():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=GEOCODE_BOSTON))

    await geocoding.search("Boston")
    await geocoding.search("boston")

    assert route.call_count == 1


@respx.mock
async def test_misses_are_cached_too():
    """A model retries a misspelling far more often than it repeats a hit."""
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=GEOCODE_EMPTY))

    assert await geocoding.search("Zzzyzx") == []
    assert await geocoding.search("Zzzyzx") == []
    assert route.call_count == 1


@respx.mock
async def test_a_different_count_is_a_different_entry():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=GEOCODE_BOSTON))

    await geocoding.search("Boston", count=2)
    await geocoding.search("Boston", count=5)

    assert route.call_count == 2


def test_cache_is_bounded(monkeypatch):
    """The key is caller-supplied text; unbounded, it is a slow memory leak."""
    monkeypatch.setattr(geocoding, "CACHE_SIZE", 4)
    for index in range(20):
        geocoding._remember((f"place{index}", 5, "en"), [])
    assert len(geocoding._cache) == 4


@respx.mock
async def test_resolve_picks_the_first_and_reports_the_rest():
    respx.get(URL).mock(return_value=httpx.Response(200, json=GEOCODE_BOSTON))

    chosen, others = await geocoding.resolve("Boston")

    assert chosen.admin1 == "Massachusetts"
    assert [o.admin1 for o in others] == ["New York"]


async def test_resolve_reads_coordinates_without_a_lookup():
    """Nothing is mocked here: a lookup would fail the test by trying to connect."""
    chosen, others = await geocoding.resolve("30.2672,-97.7431")

    assert (chosen.latitude, chosen.longitude) == (30.2672, -97.7431)
    assert others == []


async def test_resolve_rejects_impossible_coordinates_as_a_name():
    """A latitude of 200 is not coordinates, so it falls through to geocoding."""
    assert geocoding._parse_coordinates("200,-97") is None
    assert geocoding._parse_coordinates("Boston, Massachusetts") is None


@respx.mock
async def test_resolve_says_what_to_try_when_nothing_matches():
    respx.get(URL).mock(return_value=httpx.Response(200, json=GEOCODE_EMPTY))

    with pytest.raises(UnknownPlace, match="region or country"):
        await geocoding.resolve("Zzzyzx Nonesuch")


async def test_resolve_uses_home_when_no_place_is_given(home):
    chosen, others = await geocoding.resolve(None)

    assert chosen.name == "Austin"
    assert (chosen.latitude, chosen.longitude) == (30.2672, -97.7431)
    assert others == []


async def test_no_place_and_no_home_says_how_to_fix_it():
    with pytest.raises(UnknownPlace, match="WEATHER_MCP_HOME"):
        await geocoding.resolve(None)


async def test_malformed_home_is_reported_not_ignored(monkeypatch):
    """Silently falling back would forecast for somewhere the user never named."""
    monkeypatch.setenv("WEATHER_MCP_HOME", "Austin, Texas")

    with pytest.raises(WeatherError, match="latitude,longitude"):
        await geocoding.resolve(None)
