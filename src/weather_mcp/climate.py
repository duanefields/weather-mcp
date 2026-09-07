"""What the weather is *usually* like, from the Historical Weather API.

This exists because of a hard limit and a soft habit that collide. The limit:
Open-Meteo forecasts 16 days ahead and refuses anything further. The habit:
people plan trips months out and ask about the weather the moment the flights
are booked. A server that only wraps the forecast endpoint therefore says
nothing at all in exactly the situation it was built for.

So for a window beyond the horizon, the honest answer is not a forecast -- it is
what actually happened on those dates in recent years. Real observations, not a
model run, and never to be presented as a prediction. Every reply from here says
so in its first line, and the structured payload carries ``"kind": "history"``
so the distinction survives into whatever formats it.

Deliberately **not** the Climate API. That endpoint answers far-future dates
happily, but with downscaled CMIP6 climate *projections* -- numbers that read
exactly like a forecast and are not one. Observations of the last ten years are
both more truthful and more useful for "what should I pack".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from . import openmeteo
from .forecast import _check_dates, units
from .geocoding import Location
from .openmeteo import WeatherError

DEFAULT_YEARS = 10
MAX_YEARS = 30

# Open-Meteo's reanalysis archive begins here.
EARLIEST_YEAR = 1940

# One window is at most this long. A "typical weather" question is about a trip,
# not a season, and 31 days x 30 years is 930 days of arrays nobody reads.
MAX_WINDOW_DAYS = 31

DAILY_VARIABLES = [
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
]

# Below this, a day's total is a trace -- dew, a passing sprinkle -- and calling
# it a rainy day overstates the record badly. 0.01 in is the US convention for
# "measurable"; the millimeter equivalent is close enough for the same purpose.
WET_DAY_THRESHOLD = {"inch": 0.01, "mm": 0.25}


@dataclass(frozen=True)
class YearSummary:
    """One year's window, summarized both ways.

    The mean and the extreme are different questions and get separate fields.
    Collapsing them was a real bug: reporting only each year's *peak* high and
    then averaging those across years produced an "average high" several degrees
    above any typical day, which is precisely the number somebody packing for a
    trip would be misled by.
    """

    year: int
    mean_high: float | None
    mean_low: float | None
    peak_high: float | None
    lowest_low: float | None
    precipitation: float | None
    wet_days: int
    days: int
    # Read off the response rather than echoed from the request, so a summary
    # can never label Celsius numbers as Fahrenheit.
    temperature_unit: str = ""
    precipitation_unit: str = ""


def _windows(start: str, end: str, years: int) -> list[tuple[int, date, date]]:
    """The same calendar window in each of the ``years`` complete years before now.

    Complete years only -- the current year is excluded on purpose. The archive
    trails real time by a day or so, and a window that has not happened yet
    comes back as a column of nulls that would silently drag every average
    toward whatever partial data did arrive. Asking only about years that are
    entirely in the past makes that impossible rather than merely unlikely.
    """
    first, last = _check_dates(start, end)
    span = (last - first).days
    if span + 1 > MAX_WINDOW_DAYS:
        raise WeatherError(
            f"That window is {span + 1} days; typical weather is summarized for at most "
            f"{MAX_WINDOW_DAYS} days at a time."
        )

    if years < 1:
        raise WeatherError("years must be at least 1.")
    if years > MAX_YEARS:
        raise WeatherError(f"years must be at most {MAX_YEARS}.")

    latest = date.today().year - 1
    if latest - years + 1 < EARLIEST_YEAR:
        raise WeatherError(f"The archive starts in {EARLIEST_YEAR}.")

    windows = []
    for year in range(latest - years + 1, latest + 1):
        try:
            begin = first.replace(year=year)
        except ValueError:
            # 29 February in a year that has none. Skip it rather than sliding
            # to the 28th, which would double-count that day.
            continue
        windows.append((year, begin, begin + timedelta(days=span)))
    return windows


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


async def _one_year(
    location: Location, begin: date, finish: date, year: int
) -> YearSummary:
    payload = await openmeteo.get(
        openmeteo.ARCHIVE_URL,
        {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "daily": DAILY_VARIABLES,
            "timezone": "auto",
            "start_date": begin.isoformat(),
            "end_date": finish.isoformat(),
            **units(),
        },
    )

    daily = payload.get("daily") or {}
    highs = [value for value in (daily.get("temperature_2m_max") or []) if value is not None]
    lows = [value for value in (daily.get("temperature_2m_min") or []) if value is not None]
    rain = [value for value in (daily.get("precipitation_sum") or []) if value is not None]

    reported = payload.get("daily_units") or {}
    unit = reported.get("precipitation_sum", "mm")
    threshold = WET_DAY_THRESHOLD.get(unit, WET_DAY_THRESHOLD["mm"])

    return YearSummary(
        year=year,
        mean_high=_mean(highs),
        mean_low=_mean(lows),
        peak_high=max(highs) if highs else None,
        lowest_low=min(lows) if lows else None,
        precipitation=round(sum(rain), 2) if rain else None,
        wet_days=sum(1 for value in rain if value >= threshold),
        days=len(highs),
        temperature_unit=reported.get("temperature_2m_max", ""),
        precipitation_unit=unit,
    )


async def typical(
    location: Location, start: str, end: str, years: int = DEFAULT_YEARS
) -> dict[str, Any]:
    """Summarize what actually happened in this window over recent years.

    The years are fetched concurrently. Ten small requests cost one round trip's
    worth of wall time and stay far inside the free tier's 600-per-minute
    ceiling, which is the cheaper trade against one huge multi-year request that
    would download every day in between to use two weeks of it.
    """
    windows = _windows(start, end, years)
    summaries = await asyncio.gather(
        *(_one_year(location, begin, finish, year) for year, begin, finish in windows)
    )

    observed = [summary for summary in summaries if summary.days]
    if not observed:
        raise WeatherError(
            "The archive returned no observations for that window. Check the dates."
        )

    mean_highs = [s.mean_high for s in observed if s.mean_high is not None]
    mean_lows = [s.mean_low for s in observed if s.mean_low is not None]
    peaks = [s.peak_high for s in observed if s.peak_high is not None]
    troughs = [s.lowest_low for s in observed if s.lowest_low is not None]
    totals = [s.precipitation for s in observed if s.precipitation is not None]
    wet_years = [s for s in observed if s.wet_days]

    return {
        "kind": "history",
        "units": {
            "temperature": observed[0].temperature_unit,
            "precipitation": observed[0].precipitation_unit,
        },
        "years": len(observed),
        "from": observed[0].year,
        "to": observed[-1].year,
        # A typical day in this window, averaged over the years.
        "averageHigh": _mean(mean_highs),
        "averageLow": _mean(mean_lows),
        # The extremes actually recorded -- the hottest afternoon and the
        # coldest night in any of those years, not an average of anything.
        "warmest": max(peaks) if peaks else None,
        "coldest": min(troughs) if troughs else None,
        "averagePrecipitation": _mean(totals),
        "wettest": max(totals) if totals else None,
        # The number people actually want: not "how much rain on average", which
        # a single soaked year skews beyond recognition, but how often the
        # window was wet at all.
        "yearsWithRain": len(wet_years),
        "byYear": [
            {
                "year": s.year,
                "averageHigh": s.mean_high,
                "averageLow": s.mean_low,
                "warmest": s.peak_high,
                "coldest": s.lowest_low,
                "precipitation": s.precipitation,
                "wetDays": s.wet_days,
            }
            for s in observed
        ],
    }
