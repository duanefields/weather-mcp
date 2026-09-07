"""Ranking the days in a forecast window by how pleasant they are to be outside.

"What's a good day for a hike in the next two weeks" is not a forecast lookup.
It is arithmetic over 300-odd hourly readings, and it is exactly the kind of
arithmetic that gets done badly by eye: a day whose *high* is 95°F can be
perfect at 8am, and a day with a 70% chance of rain can be dry for the whole
afternoon.

Two decisions do most of the work here:

**Score the hours you would actually be out, not the whole day.** Daily
aggregates hide the shape of a day completely. `temperature_2m_max` is one
moment, usually mid-afternoon, and `precipitation_probability_max` is the worst
hour out of twenty-four including the ones spent asleep.

**Score apparent temperature, not temperature.** Open-Meteo's
`apparent_temperature` already folds in humidity, wind chill and solar
radiation, which is precisely the difference between 85°F in Austin and 85°F in
Denver. Scoring the dry-bulb number and then adding a humidity correction would
be rebuilding, worse, a thing the API already did.

The scoring is a stated preference, not a physical quantity. It is kept crude
and legible on purpose -- every penalty is in units of "degrees of discomfort",
so the weights can be read and argued with -- and every reply reports the
components alongside the verdict, because a bare score out of 100 is a number
nobody has any reason to trust.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Defaults chosen for walking around outdoors in daylight, in the configured
# temperature unit. Anything the caller cares about differently is an argument.
COMFORT_BAND_F = (50.0, 78.0)
COMFORT_BAND_C = (10.0, 25.5)

DEFAULT_START_HOUR = 8
DEFAULT_END_HOUR = 18

# Discomfort grows faster than the thermometer does. The penalty for an hour is
# the square of its distance outside the comfortable band, divided by this -- so
# 3 degrees over costs 3 points, 10 degrees costs 33, and 17 degrees costs the
# whole hundred.
#
# It is quadratic because a linear penalty, tried first, was visibly wrong: an
# Austin day running 87°F to 104°F feels-like scored 79 and came back "good" for
# a hike, because ten pleasant morning degrees averaged out a lethal afternoon.
#
# The divisor was then tightened from 4 to 3, because at 4 a sustained 88-94°F
# afternoon still came back "fair". Both calibration points are pinned in
# tests/test_comfort.py; change this and they will tell you what you changed.
HEAT_DIVISOR = 3.0

# A certainty of rain costs this many points -- equivalent to about 13 degrees
# outside the band, sustained all day.
RAIN_WEIGHT = 40.0

# Wind is pleasant up to a point and then it is not. Above this, each unit of
# speed costs a point.
WIND_TOLERANCE = 15.0

RATINGS = ((85.0, "excellent"), (70.0, "good"), (50.0, "fair"), (0.0, "poor"))


@dataclass(frozen=True)
class DayScore:
    """One day, judged over the hours the caller would be outside."""

    date: str
    score: float
    rating: str
    feels_low: float | None
    feels_high: float | None
    rain_chance: int
    precipitation: float
    max_wind: float | None
    humidity: int | None
    conditions: str
    hours: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "score": round(self.score, 1),
            "rating": self.rating,
            "feelsLikeLow": self.feels_low,
            "feelsLikeHigh": self.feels_high,
            "rainChance": self.rain_chance,
            "precipitation": round(self.precipitation, 2),
            "maxWind": self.max_wind,
            "humidity": self.humidity,
            "conditions": self.conditions,
            "hoursConsidered": self.hours,
        }


def default_band(temperature_unit: str) -> tuple[float, float]:
    """The comfortable range, in whichever unit the API is answering in."""
    return COMFORT_BAND_C if "C" in (temperature_unit or "").upper() else COMFORT_BAND_F


def _rate(score: float) -> str:
    for threshold, label in RATINGS:
        if score >= threshold:
            return label
    return "poor"


def _hour_of(iso: str) -> int:
    return int(iso.split("T", 1)[1][:2])


def _mode(values: list[str]) -> str:
    """The most common condition in the window, ties broken by first seen.

    A day that is overcast for eight hours and briefly drizzles should read as
    overcast; taking the worst hour would label most of a mild week as rain.
    """
    if not values:
        return "unknown conditions"
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(values, key=lambda value: (counts[value], -values.index(value)))


def score_days(
    hours: list[dict[str, Any]],
    start_hour: int = DEFAULT_START_HOUR,
    end_hour: int = DEFAULT_END_HOUR,
    band: tuple[float, float] = COMFORT_BAND_F,
) -> list[DayScore]:
    """Group hourly readings into days and score each one.

    ``end_hour`` is exclusive, so 8 to 18 is ten hours of daylight and not
    eleven. Days with no hours in the window -- the first day of a range that
    starts mid-afternoon, most often -- are dropped rather than scored on
    whatever fragment arrived.
    """
    low, high = band

    grouped: dict[str, list[dict[str, Any]]] = {}
    for hour in hours:
        stamp = hour.get("time") or ""
        if "T" not in stamp:
            continue
        if not (start_hour <= _hour_of(stamp) < end_hour):
            continue
        grouped.setdefault(stamp.split("T", 1)[0], []).append(hour)

    scored = []
    for date, window in sorted(grouped.items()):
        feels = [h["feelsLike"] for h in window if h.get("feelsLike") is not None]
        chances = [h.get("precipitationChance") or 0 for h in window]
        amounts = [h.get("precipitationAmount") or 0.0 for h in window]
        winds = [h["wind"] for h in window if h.get("wind") is not None]
        humidities = [h["humidity"] for h in window if h.get("humidity") is not None]

        if not feels:
            continue

        # Per-hour penalties averaged, rather than the worst hour taken: one
        # uncomfortable hour in ten does not make a day unsuitable. The squaring
        # happens per hour and before the averaging, which is what keeps a hot
        # afternoon from being cancelled out by a pleasant morning.
        discomfort = (
            sum(max(0.0, low - value, value - high) ** 2 for value in feels)
            / len(feels)
            / HEAT_DIVISOR
        )

        # The peak, not the mean. A single hour at 80% is a soaking, and
        # averaging it against a dry morning hides exactly what matters.
        peak_chance = max(chances) if chances else 0
        rain = RAIN_WEIGHT * peak_chance / 100.0

        gust = max(winds) if winds else 0.0
        wind = max(0.0, gust - WIND_TOLERANCE)

        score = max(0.0, 100.0 - discomfort - rain - wind)

        scored.append(
            DayScore(
                date=date,
                score=score,
                rating=_rate(score),
                feels_low=min(feels),
                feels_high=max(feels),
                rain_chance=peak_chance,
                precipitation=sum(amounts),
                max_wind=max(winds) if winds else None,
                humidity=round(sum(humidities) / len(humidities)) if humidities else None,
                conditions=_mode([h.get("conditions", "") for h in window]),
                hours=len(window),
            )
        )

    # Best first, and ties broken by date so the nearer of two equally good days
    # wins -- which is both more useful and stable across calls.
    return sorted(scored, key=lambda day: (-day.score, day.date))
