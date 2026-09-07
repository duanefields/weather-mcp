"""Place names to coordinates, via Open-Meteo's geocoding API.

The weather API takes coordinates and nothing else. Everything a person says --
"Boston", "the trip to Austin", a `LOCATION` copied off a calendar event -- has
to pass through here first.

Three things this API gives that matter downstream:

- **The IANA timezone.** A forecast for "the day of" is meaningless without the
  local day boundary, and the destination's zone is emphatically not the zone
  the server happens to be running in. Every result carries its own.
- **Ranked candidates.** "Springfield" is five real cities; the API returns them
  ordered by population with region and country attached, which is exactly the
  material needed to ask the user which one rather than guessing.
- **Nothing in reverse.** There is no lat/lon to name lookup -- passing
  coordinates is rejected outright -- so this module is one-directional by
  necessity, not by choice.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from . import openmeteo
from .openmeteo import WeatherError

MAX_RESULTS = 10

# Where a city is does not change. So geocoding results are cached for the life
# of the process with no expiry -- the thing a TTL exists to catch cannot
# happen here, and re-asking would only spend call budget to receive the same
# answer.
#
# It earns its place on the common path rather than the rare one: every
# forecast question names a place, and a week of "what's it doing in Austin"
# is one lookup instead of fifty. Bounded, because the key is caller-supplied
# text and an unbounded dict keyed on user input is a slow memory leak.
CACHE_SIZE = 256
_cache: OrderedDict[tuple[str, int, str], list["Location"]] = OrderedDict()


def _cached(key: tuple[str, int, str]) -> list["Location"] | None:
    hit = _cache.get(key)
    if hit is not None:
        _cache.move_to_end(key)
    return hit


def _remember(key: tuple[str, int, str], value: list["Location"]) -> None:
    _cache[key] = value
    _cache.move_to_end(key)
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)


def clear_cache() -> None:
    """Forget every cached lookup. For tests."""
    _cache.clear()


class UnknownPlace(WeatherError):
    """The geocoder found nothing matching the query."""


@dataclass(frozen=True)
class Location:
    """A resolved place: somewhere a forecast can actually be requested for."""

    name: str
    latitude: float
    longitude: float
    timezone: str
    country: str | None = None
    admin1: str | None = None
    admin2: str | None = None
    population: int | None = None
    elevation: float | None = None

    @property
    def label(self) -> str:
        """How the place is named back to the caller.

        Every reply carries this. A forecast for the wrong Springfield that does
        not say which Springfield is worse than an error, because nothing about
        it looks wrong.
        """
        parts = [self.name]
        if self.admin1 and self.admin1 != self.name:
            parts.append(self.admin1)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timeZone": self.timezone,
            "country": self.country,
            "region": self.admin1,
            "population": self.population,
        }


def _from_result(row: dict[str, Any]) -> Location:
    return Location(
        name=row["name"],
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        # Every geocoding row observed carries a zone, but a place without one
        # would otherwise produce UTC forecasts that look local and are not.
        timezone=row.get("timezone") or "UTC",
        country=row.get("country"),
        admin1=row.get("admin1"),
        admin2=row.get("admin2"),
        population=row.get("population"),
        elevation=row.get("elevation"),
    )


async def search(query: str, count: int = 5, language: str = "en") -> list[Location]:
    """Candidate places for a name, most populous first.

    Open-Meteo's own ordering is by relevance and population, and it is good --
    "Boston" puts Massachusetts ahead of the village in New York. It is left
    alone rather than re-sorted.
    """
    query = query.strip()
    if not query:
        raise UnknownPlace("A place name is required.")

    count = max(1, min(count, MAX_RESULTS))
    # Case-folded, so "boston" and "Boston" are one entry rather than two.
    key = (query.casefold(), count, language)
    hit = _cached(key)
    if hit is not None:
        return hit

    payload = await openmeteo.get(
        openmeteo.GEOCODING_URL,
        {
            "name": query,
            "count": count,
            "language": language,
            "format": "json",
        },
    )

    # A query matching nothing omits `results` entirely rather than returning [].
    matches = [_from_result(row) for row in payload.get("results") or []]

    # A miss is cached too. A misspelling gets retried by a model far more often
    # than a hit does, and each retry is a call spent to be told "no" again.
    _remember(key, matches)
    return matches


def _home_from_env() -> Location | None:
    """The location used when a caller names none.

    Coordinates rather than a name on purpose. A name would be re-geocoded on
    every call, and worse, would be re-resolved -- so an upstream change in the
    ranking of "Austin" could silently move home to Minnesota. Pinning the
    coordinates makes home a fact rather than a lookup.

    ``WEATHER_MCP_HOME`` is ``lat,lon``; ``WEATHER_MCP_HOME_NAME`` and
    ``WEATHER_MCP_HOME_TIMEZONE`` label it. Without a zone the forecast API's
    ``timezone=auto`` still resolves the right one from the coordinates, so the
    zone is optional and only affects the label.
    """
    raw = os.environ.get("WEATHER_MCP_HOME", "").strip()
    if not raw:
        return None

    try:
        lat_text, lon_text = raw.split(",", 1)
        latitude = float(lat_text)
        longitude = float(lon_text)
    except ValueError:
        raise WeatherError(
            f"WEATHER_MCP_HOME must be 'latitude,longitude' (for example '30.2672,-97.7431'); "
            f"got {raw!r}."
        ) from None

    return Location(
        name=os.environ.get("WEATHER_MCP_HOME_NAME", "").strip() or "Home",
        latitude=latitude,
        longitude=longitude,
        timezone=os.environ.get("WEATHER_MCP_HOME_TIMEZONE", "").strip() or "auto",
    )


def _parse_coordinates(text: str) -> Location | None:
    """Read ``"30.27,-97.74"`` as a location, or return None if it is a name.

    Lets a caller pass coordinates straight through where it already has them --
    from a previous ``search_locations`` call, most often -- without a second
    geocoding round trip.
    """
    parts = text.split(",")
    if len(parts) != 2:
        return None
    try:
        latitude = float(parts[0].strip())
        longitude = float(parts[1].strip())
    except ValueError:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return Location(
        name=f"{latitude:.4f}, {longitude:.4f}",
        latitude=latitude,
        longitude=longitude,
        timezone="auto",
    )


async def resolve(place: str | None) -> tuple[Location, list[Location]]:
    """Turn whatever the caller supplied into one location to forecast for.

    Returns the chosen location and the *other* candidates that matched. The
    alternatives are not decoration: they are how a tool reply can say "this is
    Boston, Massachusetts -- there is also a Boston in New York" and let the
    model check itself against what the user actually meant.

    ``None`` selects the configured home.
    """
    if place is None or not place.strip():
        home = _home_from_env()
        if home is None:
            raise UnknownPlace(
                "No location was given and no home location is configured. Name a place, "
                "or set WEATHER_MCP_HOME to 'latitude,longitude'."
            )
        return home, []

    place = place.strip()

    coordinates = _parse_coordinates(place)
    if coordinates is not None:
        return coordinates, []

    matches = await search(place, count=5)
    if not matches:
        raise UnknownPlace(
            f"No place named {place!r} was found. Try a larger nearby city, or add the "
            f"region or country, for example 'Springfield, Illinois'."
        )
    return matches[0], matches[1:]
