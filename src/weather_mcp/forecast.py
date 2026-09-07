"""Building forecast requests, and turning the answers into rows.

Open-Meteo answers in columns -- ``{"time": [...], "temperature_2m_max": [...]}``
-- which is compact on the wire and unreadable to a model, because reading day
three means indexing four parallel arrays and trusting they stayed aligned.
Everything here exists to hand back a list of days, each one a self-contained
dict.

The other job is the date window. Two ways to ask, and they are not equivalent:

``forecast_days``
    Anchored to *the location's* local today by the API itself. Correct when the
    caller means "the next week", and the only correct option when the server is
    running in a different timezone than the place being asked about -- which,
    for a server whose whole purpose is travel, is the normal case.

``start_date``/``end_date``
    Explicit ISO dates. Correct when the caller has real dates in hand, such as
    the ones on a calendar event.

Never compute "today" from the server's clock to fill in the first form. That is
how a forecast for Austin ends up starting on the wrong day because the laptop
asking for it was in Amsterdam.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from . import openmeteo, wmo
from .geocoding import Location
from .openmeteo import WeatherError

# The hard ceiling on api.open-meteo.com. Asking past it is refused upstream
# with a good message, but catching it here lets the reply say what to do
# instead -- which is ask for typical weather rather than a forecast.
MAX_FORECAST_DAYS = 16

DEFAULT_FORECAST_DAYS = 7

DAILY_VARIABLES = [
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "apparent_temperature_max",
    "apparent_temperature_min",
    "precipitation_sum",
    "precipitation_probability_max",
    "precipitation_hours",
    "wind_speed_10m_max",
    "wind_gusts_10m_max",
    "sunrise",
    "sunset",
    "uv_index_max",
]

HOURLY_VARIABLES = [
    "temperature_2m",
    "apparent_temperature",
    "precipitation_probability",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
    "relative_humidity_2m",
]

CURRENT_VARIABLES = [
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
]


def units() -> dict[str, str]:
    """Measurement units for every request.

    Defaults are American, and they are set per request rather than converted
    afterwards: Open-Meteo does the conversion server-side, so there is no
    arithmetic here to get wrong. Note the units are per family -- asking for
    Fahrenheit does not make precipitation come back in inches, which is how a
    reply ends up quietly mixing °F with millimeters.
    """
    return {
        "temperature_unit": os.environ.get("WEATHER_MCP_TEMPERATURE_UNIT", "fahrenheit"),
        "wind_speed_unit": os.environ.get("WEATHER_MCP_WIND_SPEED_UNIT", "mph"),
        "precipitation_unit": os.environ.get("WEATHER_MCP_PRECIPITATION_UNIT", "inch"),
    }


def _window(
    start: str | None, end: str | None, days: int | None
) -> dict[str, Any]:
    """Query parameters for the requested date window.

    Explicit dates win when given. Otherwise a day count, which the API anchors
    to the location's own today.
    """
    if start or end:
        if not (start and end):
            raise WeatherError(
                "Give both startDate and endDate, or neither. A single date is ambiguous: "
                "pass the same value for both to ask about one day."
            )
        first, last = _check_dates(start, end)
        return {"start_date": first.isoformat(), "end_date": last.isoformat()}

    count = DEFAULT_FORECAST_DAYS if days is None else days
    if count < 1:
        raise WeatherError("days must be at least 1.")
    if count > MAX_FORECAST_DAYS:
        raise WeatherError(
            f"Open-Meteo forecasts at most {MAX_FORECAST_DAYS} days ahead; {count} was asked "
            f"for. For a date further out than that, ask for typical weather instead -- "
            f"there is no forecast to give."
        )
    return {"forecast_days": count}


def _check_dates(start: str, end: str) -> tuple[date, date]:
    try:
        first = date.fromisoformat(start.strip())
        last = date.fromisoformat(end.strip())
    except ValueError:
        raise WeatherError(
            f"Dates must be ISO 8601, for example '2026-10-14'; got {start!r} and {end!r}."
        ) from None
    if last < first:
        raise WeatherError(f"endDate ({end}) is before startDate ({start}).")
    return first, last


def _rows(block: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Transpose one of Open-Meteo's column blocks into a list of rows.

    Shortest column wins. The arrays are always the same length in practice, but
    a mismatch would otherwise raise an IndexError deep inside formatting, where
    it reads as a bug in this server rather than a surprise from upstream.
    """
    if not block:
        return []
    times = block.get("time") or []
    keys = [key for key in block if key != "time"]
    length = min([len(times)] + [len(block[key]) for key in keys])
    return [
        {"time": times[index], **{key: block[key][index] for key in keys}}
        for index in range(length)
    ]


def _day(row: dict[str, Any]) -> dict[str, Any]:
    """One forecast day, named the way the tool surface names things."""
    code = row.get("weather_code")
    return {
        "date": row.get("time"),
        "conditions": wmo.describe(code),
        "weatherCode": code,
        "high": row.get("temperature_2m_max"),
        "low": row.get("temperature_2m_min"),
        "feelsLikeHigh": row.get("apparent_temperature_max"),
        "feelsLikeLow": row.get("apparent_temperature_min"),
        "precipitationChance": row.get("precipitation_probability_max"),
        "precipitationAmount": row.get("precipitation_sum"),
        "precipitationHours": row.get("precipitation_hours"),
        "maxWind": row.get("wind_speed_10m_max"),
        "maxGust": row.get("wind_gusts_10m_max"),
        "uvIndexMax": row.get("uv_index_max"),
        "sunrise": row.get("sunrise"),
        "sunset": row.get("sunset"),
        "wet": wmo.is_wet(code),
        "severe": wmo.is_severe(code),
    }


def _hour(row: dict[str, Any]) -> dict[str, Any]:
    code = row.get("weather_code")
    return {
        "time": row.get("time"),
        "conditions": wmo.describe(code),
        "weatherCode": code,
        "temperature": row.get("temperature_2m"),
        "feelsLike": row.get("apparent_temperature"),
        "precipitationChance": row.get("precipitation_probability"),
        "precipitationAmount": row.get("precipitation"),
        "wind": row.get("wind_speed_10m"),
        "humidity": row.get("relative_humidity_2m"),
        "wet": wmo.is_wet(code),
    }


def _current(payload: dict[str, Any]) -> dict[str, Any] | None:
    block = payload.get("current")
    if not block:
        return None
    code = block.get("weather_code")
    return {
        "time": block.get("time"),
        "conditions": wmo.describe(code),
        "weatherCode": code,
        "temperature": block.get("temperature_2m"),
        "feelsLike": block.get("apparent_temperature"),
        "humidity": block.get("relative_humidity_2m"),
        "precipitation": block.get("precipitation"),
        "wind": block.get("wind_speed_10m"),
    }


def _units_of(payload: dict[str, Any]) -> dict[str, str]:
    """The units the API actually answered in.

    Read back off the response rather than echoing what was requested, so a
    reply can never claim °F for numbers that came back in °C.
    """
    # Both blocks, merged. An hourly request also asks for sunrise and sunset,
    # which means it gets a `daily_units` carrying neither a temperature nor a
    # precipitation key -- so reading only the first block that exists left every
    # hourly reading unlabeled, printing "59" and "0.07 " where "59°F" and
    # "0.07 inch" belonged.
    reported = {**(payload.get("hourly_units") or {}), **(payload.get("daily_units") or {})}
    return {
        "temperature": reported.get("temperature_2m_max")
        or reported.get("temperature_2m")
        or "",
        "precipitation": reported.get("precipitation_sum")
        or reported.get("precipitation")
        or "",
        "wind": reported.get("wind_speed_10m_max") or reported.get("wind_speed_10m") or "",
    }


async def daily(
    location: Location,
    start: str | None = None,
    end: str | None = None,
    days: int | None = None,
    include_current: bool = True,
) -> dict[str, Any]:
    """Day-by-day forecast for a location."""
    params: dict[str, Any] = {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "daily": DAILY_VARIABLES,
        "timezone": "auto",
        **units(),
        **_window(start, end, days),
    }
    if include_current:
        params["current"] = CURRENT_VARIABLES

    payload = await openmeteo.get(openmeteo.FORECAST_URL, params)

    return {
        "timeZone": payload.get("timezone"),
        "elevation": payload.get("elevation"),
        "units": _units_of(payload),
        "current": _current(payload),
        "days": [_day(row) for row in _rows(payload.get("daily"))],
    }


async def hourly(location: Location, day: str) -> dict[str, Any]:
    """Hour-by-hour forecast for one local day at a location."""
    _check_dates(day, day)
    payload = await openmeteo.get(
        openmeteo.FORECAST_URL,
        {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "hourly": HOURLY_VARIABLES,
            "daily": ["sunrise", "sunset", "weather_code"],
            "timezone": "auto",
            "start_date": day,
            "end_date": day,
            **units(),
        },
    )

    days = _rows(payload.get("daily"))
    return {
        "timeZone": payload.get("timezone"),
        "units": _units_of(payload),
        "sunrise": days[0].get("sunrise") if days else None,
        "sunset": days[0].get("sunset") if days else None,
        "hours": [_hour(row) for row in _rows(payload.get("hourly"))],
    }


async def hourly_range(
    location: Location,
    start: str | None = None,
    end: str | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    """Hourly readings across a multi-day window, for scoring rather than display.

    One request covers the whole range -- sixteen days is 384 rows, which is
    large to print and trivial to fetch. Nothing here is meant to be shown to
    anyone; ``comfort.score_days`` reduces it to one row per day.
    """
    payload = await openmeteo.get(
        openmeteo.FORECAST_URL,
        {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "hourly": HOURLY_VARIABLES,
            "timezone": "auto",
            **units(),
            **_window(start, end, days),
        },
    )

    return {
        "timeZone": payload.get("timezone"),
        "units": _units_of(payload),
        "hours": [_hour(row) for row in _rows(payload.get("hourly"))],
    }
