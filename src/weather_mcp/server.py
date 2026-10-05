"""The MCP server: tool definitions, health endpoint, and transport selection.

Read-only throughout. There is no write path, no credential, and nothing here
can change anything about the world -- which is why there is no equivalent of
dav-mcp's ``_last_write`` bookkeeping and no write check in ``/health``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import platform
import time
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from pydantic import Field
from starlette.responses import JSONResponse

from . import climate, comfort, forecast, geocoding
from .geocoding import Location
from .openmeteo import WeatherError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "Weather",
    instructions=(
        "This server answers questions about the weather anywhere in the world, "
        "from Open-Meteo: forecasts up to 16 days ahead, hour by hour for any "
        "single day in that window, and what the weather has actually been like "
        "on given dates in past years.\n\n"
        "Its purpose is practical planning -- what to expect at home, and what "
        "to expect somewhere the user is traveling to. When a trip comes up, "
        "the destination and dates are usually already known from a calendar "
        "event or the conversation; use them rather than asking again.\n\n"
        "Every tool takes a place as ordinary text and resolves it, and every "
        "reply names the place it actually resolved to. Read that back: "
        "'Springfield' and 'Portland' are several real cities, and a forecast "
        "for the wrong one looks exactly like a forecast for the right one. "
        "When the reply lists other candidates and the context does not settle "
        "which was meant, ask.\n\n"
        "When the question is which day to pick out of a range rather than what "
        "a chosen day looks like -- a hike, a ride, anything outdoors -- use "
        "find_best_days. It scores only the hours actually spent outside and "
        "uses feels-like temperature, so it will not be reproduced correctly by "
        "reading daily highs off a forecast.\n\n"
        "The 16-day horizon is a hard limit of the data, not a setting. For a "
        "trip further out than that there is no forecast and nothing can "
        "produce one. get_typical_weather answers a different question -- what "
        "the weather actually did on those dates in recent years -- and its "
        "results are history, never a prediction. Do not describe them as a "
        "forecast, and do not let them stand in for one once the dates come "
        "within range."
    ),
)


# ----------------------------------------------------------------------
# Response shaping
# ----------------------------------------------------------------------


# Below this, a chance of rain is not worth changing a plan over. The live
# forecast made the case: an Austin week with 8-14% chances every day and a
# drizzle weather code on two of them was reporting "rain worth planning
# around" for a 10% day, which trains a reader to ignore the line entirely.
NOTABLE_RAIN_CHANCE = 40


def _error_result(message: str) -> ToolResult:
    """A failure the model should read and act on, in both channels.

    Tools return this rather than raising: a raised exception reaches the model
    as an opaque failure it cannot do anything about, while a message naming the
    problem -- an unknown place, a date past the horizon -- tells it what to try
    instead.
    """
    return ToolResult(content=message, structured_content={"error": message})


def _result(text: str, structured: dict[str, Any]) -> ToolResult:
    return ToolResult(content=text, structured_content=structured)


def _number(value: Any, unit: str = "", digits: int = 0) -> str:
    """Format a measurement, or say it is missing rather than printing None."""
    if value is None:
        return "n/a"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}{unit}"
    return f"{value}{unit}"


def _place_line(chosen: Location, others: list[Location]) -> str:
    """Name the resolved place, and any others the query also matched.

    The alternatives are printed rather than merely returned in the structured
    payload, because the text channel is what a model reads most carefully and
    this is the one thing about a weather reply that can be silently wrong.
    """
    line = chosen.label
    if others:
        line += " (also matched: " + "; ".join(other.label for other in others[:3]) + ")"
    return line


def _clock(iso: str | None) -> str:
    """The time out of an ISO timestamp, in the location's own zone."""
    if not iso or "T" not in iso:
        return iso or "n/a"
    return iso.split("T", 1)[1][:5]


async def _resolve(place: str | None) -> tuple[Location, list[Location]]:
    return await geocoding.resolve(place)


# ----------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------


@mcp.tool
async def search_locations(
    query: Annotated[str, Field(description="A place name, optionally with region or country.")],
    limit: Annotated[int, Field(description="How many candidates to return.", ge=1, le=10)] = 5,
) -> ToolResult:
    """Find the places a name could refer to, with coordinates and timezone.

    Use this when a place name is ambiguous and the context does not settle it --
    'Springfield' is five US cities, 'Portland' two large ones - or when you want
    coordinates to pass to the other tools directly.

    The other tools accept a place name themselves and do not need this first.
    Reach for it to disambiguate, not as a routine preliminary step.

    Args:
        query: A place name, such as "Boston", "Springfield, Illinois", or "Kyoto".
        limit: Maximum number of candidates.
    """
    try:
        matches = await geocoding.search(query, count=limit)
    except WeatherError as exc:
        return _error_result(str(exc))

    if not matches:
        return _result(
            f"No place named {query!r} was found. Try a larger nearby city, or add the "
            f"region or country.",
            {"query": query, "locations": []},
        )

    lines = [f"{len(matches)} place(s) matching {query!r}:", ""]
    for match in matches:
        detail = f"  {match.latitude:.4f}, {match.longitude:.4f} — {match.timezone}"
        if match.population:
            detail += f" — population {match.population:,}"
        lines.append(f"- {match.label}")
        lines.append(detail)

    return _result(
        "\n".join(lines),
        {"query": query, "locations": [match.to_dict() for match in matches]},
    )


@mcp.tool
async def get_forecast(
    location: Annotated[
        str | None,
        Field(description="Place name or 'latitude,longitude'. Omit for the home location."),
    ] = None,
    startDate: Annotated[
        str | None, Field(description="First day, ISO 8601 (YYYY-MM-DD).")
    ] = None,
    endDate: Annotated[str | None, Field(description="Last day, ISO 8601 (YYYY-MM-DD).")] = None,
    days: Annotated[
        int | None,
        Field(description="Days ahead from today at the location, when no dates are given.", ge=1, le=16),
    ] = None,
) -> ToolResult:
    """Daily weather forecast for a place: highs, lows, rain and wind, day by day.

    The main tool. Answers "what is the weather doing this week", "how warm will
    it be in Boston next Tuesday", and "will it rain while I am there".

    Give dates when you have them -- from a calendar event, say -- and they are
    read as local dates at the destination, not wherever this server is running.
    Give neither and it covers the next week starting from today *at that
    location*, which is the right anchor when the place is in another timezone.

    Forecasts reach at most 16 days ahead; that is a limit of the data. For a
    trip further out, use get_typical_weather, which reports what the weather
    actually did on those dates in past years rather than pretending to predict.

    Args:
        location: Place name such as "Austin" or "Boston, Massachusetts", or
            "30.2672,-97.7431". Omit to use the configured home location.
        startDate: First day of the range, ISO 8601. Pass with endDate.
        endDate: Last day of the range, ISO 8601. Pass the same value as
            startDate to ask about a single day.
        days: How many days ahead to cover when no dates are given. Default 7.
    """
    try:
        chosen, others = await _resolve(location)
        report = await forecast.daily(chosen, start=startDate, end=endDate, days=days)
    except WeatherError as exc:
        return _error_result(str(exc))

    if not report["days"]:
        return _error_result("Open-Meteo returned no days for that range.")

    units = report["units"]
    temperature = units.get("temperature", "")
    depth = units.get("precipitation", "")
    speed = units.get("wind", "")

    lines = [f"Forecast for {_place_line(chosen, others)}", f"Times are {report['timeZone']}.", ""]

    current = report.get("current")
    if current:
        lines.append(
            f"Now ({_clock(current['time'])}): {current['conditions']}, "
            f"{_number(current['temperature'], temperature)}"
            f" (feels like {_number(current['feelsLike'], temperature)}), "
            f"wind {_number(current['wind'], ' ' + speed)}."
        )
        lines.append("")

    wet_days = []
    for day in report["days"]:
        parts = [
            f"{day['date']}: {day['conditions']}",
            f"{_number(day['low'], temperature)}–{_number(day['high'], temperature)}",
        ]
        chance = day["precipitationChance"]
        if chance:
            amount = day["precipitationAmount"]
            rain = f"{_number(chance, '%')} chance"
            if amount:
                rain += f", {_number(amount, ' ' + depth, 2)}"
            parts.append(rain)
            if chance >= NOTABLE_RAIN_CHANCE:
                wet_days.append(day)
        if day["severe"]:
            parts.append("SEVERE")
            if day not in wet_days:
                wet_days.append(day)
        lines.append("- " + " — ".join(parts))

    if wet_days:
        lines.append("")
        lines.append(
            "Rain worth planning around: "
            + ", ".join(f"{day['date']} ({_number(day['precipitationChance'], '%')})" for day in wet_days)
            + "."
        )

    return _result(
        "\n".join(lines),
        {
            "location": chosen.to_dict(),
            "alternatives": [other.to_dict() for other in others],
            "timeZone": report["timeZone"],
            "units": units,
            "current": current,
            "days": report["days"],
        },
    )


@mcp.tool
async def get_hourly_forecast(
    date: Annotated[str, Field(description="The day to break down, ISO 8601 (YYYY-MM-DD).")],
    location: Annotated[
        str | None,
        Field(description="Place name or 'latitude,longitude'. Omit for the home location."),
    ] = None,
) -> ToolResult:
    """Hour-by-hour forecast for one day: when exactly the rain arrives.

    Use this once the day matters more than the week -- "will it rain during the
    2pm walk", "is the morning or the afternoon better for the drive". For a
    multi-day outlook use get_forecast instead; this returns 24 rows and is
    wasteful for a question about a week.

    The day must be within the 16-day forecast horizon, and is read as a local
    date at the location.

    Args:
        date: The day to break down, ISO 8601 (YYYY-MM-DD).
        location: Place name or "latitude,longitude". Omit to use home.
    """
    try:
        chosen, others = await _resolve(location)
        report = await forecast.hourly(chosen, date)
    except WeatherError as exc:
        return _error_result(str(exc))

    hours = report["hours"]
    if not hours:
        return _error_result(
            f"No hourly forecast is available for {date}. It is likely outside the 16-day "
            f"horizon."
        )

    units = report["units"]
    temperature = units.get("temperature", "")
    depth = units.get("precipitation", "")

    lines = [
        f"{date} in {_place_line(chosen, others)}",
        f"Times are {report['timeZone']}. "
        f"Sunrise {_clock(report['sunrise'])}, sunset {_clock(report['sunset'])}.",
        "",
    ]
    for hour in hours:
        row = (
            f"{_clock(hour['time'])}  {_number(hour['temperature'], temperature):>6}  "
            f"{hour['conditions']}"
        )
        chance = hour["precipitationChance"]
        if chance:
            row += f" — {_number(chance, '%')} chance"
            if hour["precipitationAmount"]:
                row += f", {_number(hour['precipitationAmount'], ' ' + depth, 2)}"
        lines.append(row)

    # Contiguous wet stretches, so the reply can say "rain 2pm-5pm" rather than
    # leaving the model to scan 24 rows and infer it. This is the actual question
    # behind almost every hourly lookup.
    windows = _wet_windows(hours)
    if windows:
        lines.append("")
        lines.append(
            "Rain likely: "
            + ", ".join(f"{start}–{end}" for start, end in windows)
            + "."
        )

    return _result(
        "\n".join(lines),
        {
            "location": chosen.to_dict(),
            "alternatives": [other.to_dict() for other in others],
            "date": date,
            "timeZone": report["timeZone"],
            "units": units,
            "sunrise": report["sunrise"],
            "sunset": report["sunset"],
            "wetWindows": [{"from": start, "to": end} for start, end in windows],
            "hours": hours,
        },
    )


def _avoid_reason(day, unit: str, low: float, high: float) -> str:
    """Why a day scored poorly, in the terms that actually drove the score."""
    reasons = []
    if day.feels_high is not None and day.feels_high > high:
        reasons.append(f"feels like {_number(day.feels_high, unit)}")
    elif day.feels_low is not None and day.feels_low < low:
        reasons.append(f"feels like {_number(day.feels_low, unit)}")
    if day.rain_chance >= NOTABLE_RAIN_CHANCE:
        reasons.append(f"{day.rain_chance}% rain")
    return ", ".join(reasons) or day.conditions


def _wet_windows(hours: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Contiguous runs of hours likely enough to be worth avoiding.

    The end of a run is the *end* of its last hour, so a single wet hour at 14:00
    reads as "14:00-15:00" rather than the zero-length "14:00-14:00".
    """
    windows: list[tuple[str, str]] = []
    run: list[dict[str, Any]] = []

    def flush() -> None:
        if run:
            start = _clock(run[0]["time"])
            end_hour = int(_clock(run[-1]["time"])[:2]) + 1
            windows.append((start, f"{end_hour:02d}:00"))
            run.clear()

    for hour in hours:
        chance = hour.get("precipitationChance") or 0
        if chance >= NOTABLE_RAIN_CHANCE or hour.get("wet"):
            run.append(hour)
        else:
            flush()
    flush()
    return windows


@mcp.tool
async def get_typical_weather(
    startDate: Annotated[str, Field(description="First day of the window, ISO 8601.")],
    endDate: Annotated[str, Field(description="Last day of the window, ISO 8601.")],
    location: Annotated[
        str | None,
        Field(description="Place name or 'latitude,longitude'. Omit for the home location."),
    ] = None,
    years: Annotated[
        int, Field(description="How many past years to summarize.", ge=1, le=30)
    ] = 10,
) -> ToolResult:
    """What the weather actually did on these dates in past years. Not a forecast.

    For trips beyond the 16-day forecast horizon, which is most trips at the
    point they get planned. Returns observed history: the average high and low
    for this window over recent years, the extremes, and how many of those years
    saw rain during it.

    This is history and must be described as history. It says what to pack, not
    what will happen. Once the dates come within 16 days, get_forecast replaces
    it entirely — do not keep quoting averages when a real forecast exists.

    The year is taken from the dates only to fix the calendar window; the summary
    always covers complete past years.

    Args:
        startDate: First day of the window, ISO 8601. The year is ignored except
            to set the month and day.
        endDate: Last day of the window, ISO 8601. At most 31 days after start.
        location: Place name or "latitude,longitude". Omit to use home.
        years: How many past years to summarize. Default 10.
    """
    try:
        chosen, others = await _resolve(location)
        summary = await climate.typical(chosen, startDate, endDate, years=years)
    except WeatherError as exc:
        return _error_result(str(exc))

    units = summary["units"]
    temperature = units.get("temperature", "")
    depth = units.get("precipitation", "")

    lines = [
        f"Typical weather for {startDate[5:]} to {endDate[5:]} in "
        f"{_place_line(chosen, others)}",
        f"This is what actually happened in {summary['from']}–{summary['to']} "
        f"({summary['years']} years), not a forecast.",
        "",
        f"- Average high {_number(summary['averageHigh'], temperature, 1)}, "
        f"average low {_number(summary['averageLow'], temperature, 1)}",
        f"- Hottest recorded {_number(summary['warmest'], temperature, 1)}, "
        f"coldest {_number(summary['coldest'], temperature, 1)}",
        f"- Rain during this window in {summary['yearsWithRain']} of "
        f"{summary['years']} years",
        f"- Average total precipitation {_number(summary['averagePrecipitation'], ' ' + depth, 2)}, "
        f"wettest year {_number(summary['wettest'], ' ' + depth, 2)}",
    ]

    return _result(
        "\n".join(lines),
        {
            "location": chosen.to_dict(),
            "alternatives": [other.to_dict() for other in others],
            **summary,
        },
    )


@mcp.tool
async def find_best_days(
    location: Annotated[
        str | None,
        Field(description="Place name or 'latitude,longitude'. Omit for the home location."),
    ] = None,
    days: Annotated[
        int, Field(description="How many days ahead to consider.", ge=1, le=16)
    ] = 14,
    startHour: Annotated[
        int, Field(description="First hour to consider, 24-hour local time.", ge=0, le=23)
    ] = 8,
    endHour: Annotated[
        int, Field(description="Last hour to consider, exclusive, 24-hour local time.", ge=1, le=24)
    ] = 18,
    minTemperature: Annotated[
        float | None,
        Field(description="Coldest comfortable feels-like temperature. Defaults to 50°F / 10°C."),
    ] = None,
    maxTemperature: Annotated[
        float | None,
        Field(description="Warmest comfortable feels-like temperature. Defaults to 78°F / 25.5°C."),
    ] = None,
    limit: Annotated[int, Field(description="How many days to return.", ge=1, le=16)] = 5,
) -> ToolResult:
    """Rank the days ahead by how pleasant it will be outside. For planning a hike or similar.

    Answers "when should I go", not "what is the weather". Use it when the user
    wants a day picked out of a range — a hike, a ride, a picnic, painting the
    fence — rather than the outlook for a day they have already chosen.

    Days are scored over the hours actually spent outside (8am to 6pm by
    default), not over the whole day, because a daily high is one afternoon
    moment and a daily rain chance includes the hours you were asleep. The
    temperature scored is feels-like, which already accounts for humidity, wind
    chill and sun — so a muggy 85°F is correctly ranked below a dry one.

    The ranking is a stated preference, not a measurement. Every day comes back
    with the numbers behind it — feels-like range, rain chance, wind, humidity —
    so report those alongside the pick rather than the score alone.

    Args:
        location: Place name or "latitude,longitude". Omit to use home.
        days: How many days ahead to consider. Default 14, at most 16.
        startHour: First hour of the day to consider, local time. Default 8.
        endHour: Last hour, exclusive. Default 18. Use 6 and 11 for mornings.
        minTemperature: Coldest comfortable feels-like temperature, in the
            server's configured unit. Defaults to 50°F.
        maxTemperature: Warmest comfortable feels-like temperature. Defaults to 78°F.
        limit: How many days to list. Default 5.
    """
    if endHour <= startHour:
        return _error_result(
            f"endHour ({endHour}) must be after startHour ({startHour}). The window is the "
            f"hours you would be outside, for example 8 to 18."
        )

    try:
        chosen, others = await _resolve(location)
        report = await forecast.hourly_range(chosen, days=days)
    except WeatherError as exc:
        return _error_result(str(exc))

    units = report["units"]
    temperature = units.get("temperature", "")
    low, high = comfort.default_band(temperature)
    if minTemperature is not None:
        low = minTemperature
    if maxTemperature is not None:
        high = maxTemperature
    if low >= high:
        return _error_result(
            f"minTemperature ({low}) must be below maxTemperature ({high})."
        )

    ranked = comfort.score_days(report["hours"], startHour, endHour, (low, high))
    if not ranked:
        return _error_result(
            f"No hours between {startHour}:00 and {endHour}:00 were forecast in that range."
        )

    depth = units.get("precipitation", "")
    speed = units.get("wind", "")

    lines = [
        f"Best days to be outside in {_place_line(chosen, others)}",
        f"Scored over {startHour:02d}:00–{endHour:02d}:00 {report['timeZone']}, "
        f"comfortable between {_number(low, temperature)} and {_number(high, temperature)} "
        f"feels-like.",
        "",
    ]
    # A "best days" list that pads itself out to `limit` with days scored zero
    # is actively misleading -- the fifth entry reads as an option. Poor days are
    # named below, as days to avoid, which is what they are.
    worth_it = [day for day in ranked if day.rating != "poor"]
    if not worth_it:
        lines.append(
            f"No day in the next {days} scores better than poor. The least bad is "
            f"{ranked[0].date}."
        )
        lines.append("")

    for rank, day in enumerate(worth_it[:limit], start=1):
        lines.append(f"{rank}. {day.date} — {day.rating} ({day.score:.0f}/100)")
        detail = (
            f"   feels like {_number(day.feels_low, temperature)}–"
            f"{_number(day.feels_high, temperature)}, {day.conditions}"
        )
        if day.rain_chance:
            detail += f", {day.rain_chance}% chance of rain"
            if day.precipitation:
                detail += f" ({_number(day.precipitation, ' ' + depth, 2)})"
        else:
            detail += ", no rain expected"
        if day.max_wind:
            detail += f", wind to {_number(day.max_wind, ' ' + speed)}"
        if day.humidity is not None:
            detail += f", {day.humidity}% humidity"
        lines.append(detail)

    # The days to steer away from are worth saying out loud. A ranked list alone
    # invites reading the fifth entry as "fine", when it may be the least bad of
    # a uniformly wet fortnight.
    poor = [day for day in ranked if day.rating == "poor"]
    if poor:
        lines.append("")
        lines.append(
            "Worth avoiding: "
            + ", ".join(
                f"{day.date} ({_avoid_reason(day, temperature, low, high)})" for day in poor[:5]
            )
            + "."
        )

    return _result(
        "\n".join(lines),
        {
            "location": chosen.to_dict(),
            "alternatives": [other.to_dict() for other in others],
            "timeZone": report["timeZone"],
            "units": units,
            "window": {"startHour": startHour, "endHour": endHour},
            "comfortBand": {"min": low, "max": high},
            "days": [day.to_dict() for day in ranked],
        },
    )


# ----------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------


# /health is unauthenticated and the hostname is public within hours of going
# live, so anything on the internet can poll it as fast as it likes. Each miss
# costs a real Open-Meteo call, and the free tier is a budget shared with every
# actual tool call -- so without a cache an outsider can exhaust the day's
# allowance and take every forecast down with it.
HEALTH_TTL_SECONDS = 60.0

_health_cache: tuple[float, tuple[bool, str | None]] | None = None
_health_lock = asyncio.Lock()


async def _probe() -> tuple[bool, str | None]:
    """Ask Open-Meteo for one number, at most once per ``HEALTH_TTL_SECONDS``.

    The lock is held across the request on purpose: it collapses a concurrent
    burst into one upstream call rather than letting every caller that arrives
    before the cache fills start its own.
    """
    global _health_cache
    async with _health_lock:
        now = time.time()
        if _health_cache and now - _health_cache[0] < HEALTH_TTL_SECONDS:
            return _health_cache[1]

        try:
            # Deliberately the smallest possible request: one variable, one day.
            await forecast.daily(
                Location(name="probe", latitude=0.0, longitude=0.0, timezone="UTC"),
                days=1,
                include_current=False,
            )
            result: tuple[bool, str | None] = (True, None)
        except Exception as exc:  # the class is enough; a message may carry detail
            result = (False, type(exc).__name__)

        _health_cache = (time.time(), result)
        return result


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    """Unauthenticated health report, for monitoring a remote deployment.

    Publishes nothing about the operator -- not the home coordinates, not a
    place name. A tunnel hostname shows up in certificate transparency logs
    within hours and gets scanned, so this endpoint is effectively public.

    ``home_configured`` is a boolean rather than the location itself for exactly
    that reason: an operator needs to know the default is set, and nobody else
    needs to know where it points.

    The verdict is made here, so the monitor only has to read the status code:
    200 with ``status`` "ok", or 503 with ``status`` naming every problem found,
    comma-separated. The other fields are there for whoever curls it next.
    """
    reachable, detail = await _probe()
    home_configured = bool(os.environ.get("WEATHER_MCP_HOME", "").strip())

    problems = []
    # There is no credential to be revoked here, so the failure modes are
    # narrower than a server with an account behind it: upstream is down, the
    # free tier is exhausted, or the host lost DNS.
    if not reachable:
        problems.append(f"Open-Meteo unreachable ({detail})")
    # Every tool still works when a place is named explicitly, but this silently
    # breaks every question about "here", which is most of them.
    if not home_configured:
        problems.append("no home location configured")

    return JSONResponse(
        {
            "status": ", ".join(problems) or "ok",
            "openmeteo_reachable": reachable,
            "error": detail,
            "home_configured": home_configured,
            "python_version": platform.python_version(),
        },
        status_code=503 if problems else 200,
    )


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def _is_loopback(host: str) -> bool:
    """Whether binding to ``host`` keeps the port on this machine."""
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host.strip().lower() in ("localhost", "")


def main():
    """Run the server on the transport the environment selects."""
    # Read transport settings here rather than at import time so that a service
    # manager and the tests can set the environment before calling main().
    transport = os.environ.get("WEATHER_MCP_TRANSPORT", "stdio")
    if transport == "http":
        from .auth import build_auth

        host = os.environ.get("WEATHER_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("WEATHER_MCP_PORT", "18791"))
        # Authentication applies to the HTTP transport only; stdio inherits its
        # security from local execution.
        mcp.auth = build_auth()
        if mcp.auth is None and not _is_loopback(host):
            # Not fatal: a host behind a tunnel that does its own authentication
            # is a legitimate setup, and refusing to start would break it. The
            # data here is public weather, so an open port leaks nothing about
            # the operator -- but it does hand out their Open-Meteo call budget,
            # and the free tier has no uptime guarantee to spare.
            logger.warning(
                "Serving on %s with WEATHER_MCP_AUTH unset: this port is reachable "
                "beyond this machine, and anyone who finds it can spend the free-tier "
                "call budget this server depends on. Set WEATHER_MCP_AUTH=password "
                "unless something in front of the server is authenticating.",
                host,
            )
        # Stateless: a fresh transport per request, so there is no session for a
        # client to lose. Remote clients dial from a pool of addresses, and a
        # request arriving from a different address than the one that opened the
        # session is rejected with a 400. Nothing here needs session state.
        stateless = os.environ.get("WEATHER_MCP_STATELESS", "true").strip().lower() != "false"
        mcp.run(transport="http", host=host, port=port, stateless_http=stateless)
    else:
        mcp.run()


__all__ = ["main", "mcp"]
