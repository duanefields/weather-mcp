"""WMO 4677 weather codes, and the plain-English readings of them.

Open-Meteo reports conditions as a bare integer. On its own that number is
useless to a model and worse than useless to a person reading a tool reply, so
every code is translated at the edge and the integer is never surfaced alone.

The vocabulary is the World Meteorological Organization's, reduced to the subset
Open-Meteo actually emits (documented on their forecast API page). Codes not
listed here fall back to "unknown conditions" rather than raising: a new code
appearing upstream should degrade one field, not fail the whole call.
"""

from __future__ import annotations

DESCRIPTIONS: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "heavy drizzle",
    56: "light freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "moderate snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "light snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with light hail",
    99: "thunderstorm with heavy hail",
}

# Codes that mean water is falling out of the sky. Used to answer "will it rain
# on the day" without the caller having to know the code table -- and kept
# separate from a precipitation *amount* test, because a forecast can carry a
# rain code with a 0.0 mm total and a trace of drizzle still ruins a picnic.
WET_CODES = frozenset(
    {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99}
)

# Conditions worth saying out loud even when the rest of the day looks fine.
SEVERE_CODES = frozenset({65, 67, 75, 82, 86, 95, 96, 99})


def describe(code: int | None) -> str:
    """Plain-English reading of a WMO code."""
    if code is None:
        return "unknown conditions"
    return DESCRIPTIONS.get(int(code), f"unknown conditions (WMO {int(code)})")


def is_wet(code: int | None) -> bool:
    """Whether the code means precipitation of any kind."""
    return code is not None and int(code) in WET_CODES


def is_severe(code: int | None) -> bool:
    """Whether the code is worth flagging on its own."""
    return code is not None and int(code) in SEVERE_CODES
