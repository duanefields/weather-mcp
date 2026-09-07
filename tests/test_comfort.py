"""The day scoring. Pure arithmetic over hourly rows -- no network, no clock."""

from weather_mcp import comfort


def hour(time, feels, chance=0, amount=0.0, wind=5.0, humidity=50, conditions="clear sky"):
    return {
        "time": time,
        "feelsLike": feels,
        "precipitationChance": chance,
        "precipitationAmount": amount,
        "wind": wind,
        "humidity": humidity,
        "conditions": conditions,
    }


def day(date, feels, **kwargs):
    """A row per hour from 08:00, so the default 8-18 window covers all of them."""
    return [hour(f"{date}T{8 + i:02d}:00", value, **kwargs) for i, value in enumerate(feels)]


def test_a_comfortable_day_scores_full_marks():
    [scored] = comfort.score_days(day("2026-09-18", [62.0, 66.0, 70.0, 68.0]))

    assert scored.score == 100.0
    assert scored.rating == "excellent"


def test_hours_outside_the_window_are_ignored():
    """A daily high at 3am is not a reason to skip a hike."""
    hours = day("2026-09-18", [62.0, 66.0]) + [hour("2026-09-18T03:00", 105.0)]

    [scored] = comfort.score_days(hours)

    assert scored.score == 100.0
    assert scored.hours == 2


def test_a_pleasant_morning_does_not_rescue_a_lethal_afternoon():
    """The bug a linear penalty produced: an Austin day running 87F to 104F
    scored 79 and came back "good" for a hike."""
    [scored] = comfort.score_days(day("2026-09-07", [87.0, 92.0, 98.0, 104.0]))

    assert scored.rating == "poor"
    assert scored.score < 30


def test_a_warm_but_bearable_day_is_not_condemned():
    """Warm still beats hot, and both still count as options."""
    warm = comfort.score_days(day("2026-09-20", [74.0, 80.0, 84.0, 82.0]))[0]
    perfect = comfort.score_days(day("2026-09-21", [64.0, 66.0, 70.0, 68.0]))[0]

    assert warm.rating != "poor"
    assert warm.score < perfect.score


def test_cold_is_penalized_the_same_way_as_heat():
    [scored] = comfort.score_days(day("2026-01-10", [20.0, 24.0, 28.0, 26.0]))

    assert scored.rating == "poor"


def test_rain_uses_the_peak_hour_not_the_average():
    """One hour at 80% is a soaking; averaging it against a dry morning hides it."""
    hours = day("2026-09-18", [65.0, 65.0, 65.0, 65.0])
    hours[2]["precipitationChance"] = 80

    [scored] = comfort.score_days(hours)

    assert scored.rain_chance == 80
    assert scored.score == 100.0 - comfort.RAIN_WEIGHT * 0.8


def test_wind_only_counts_above_the_tolerance():
    calm = comfort.score_days(day("2026-09-18", [65.0, 65.0], wind=10.0))[0]
    gale = comfort.score_days(day("2026-09-19", [65.0, 65.0], wind=35.0))[0]

    assert calm.score == 100.0
    assert gale.score == 80.0


def test_conditions_report_the_common_case_not_the_worst_hour():
    """A mostly overcast day that drizzles once should read as overcast."""
    hours = day("2026-09-18", [65.0] * 5, conditions="overcast")
    hours[3]["conditions"] = "light drizzle"

    [scored] = comfort.score_days(hours)

    assert scored.conditions == "overcast"


def test_days_are_ranked_best_first_then_soonest():
    hours = (
        day("2026-09-20", [65.0, 65.0])
        + day("2026-09-18", [65.0, 65.0])
        + day("2026-09-19", [95.0, 99.0])
    )

    ranked = comfort.score_days(hours)

    assert [d.date for d in ranked] == ["2026-09-18", "2026-09-20", "2026-09-19"]


def test_a_custom_window_selects_different_hours():
    hours = day("2026-09-18", [55.0, 58.0, 62.0, 90.0, 95.0, 99.0])

    [morning] = comfort.score_days(hours, start_hour=8, end_hour=11)

    assert morning.hours == 3
    assert morning.rating == "excellent"


def test_a_day_with_no_hours_in_the_window_is_dropped():
    """A range starting mid-afternoon should not be scored on its fragment."""
    hours = [hour("2026-09-18T22:00", 65.0), hour("2026-09-18T23:00", 65.0)]

    assert comfort.score_days(hours, start_hour=8, end_hour=18) == []


def test_the_band_follows_the_configured_unit():
    assert comfort.default_band("°C") == comfort.COMFORT_BAND_C
    assert comfort.default_band("°F") == comfort.COMFORT_BAND_F
    assert comfort.default_band("") == comfort.COMFORT_BAND_F


def test_a_custom_band_changes_the_verdict():
    """Somebody who likes it hot should be able to say so."""
    hours = day("2026-09-07", [88.0, 92.0, 94.0])

    default = comfort.score_days(hours)[0]
    tolerant = comfort.score_days(hours, band=(70.0, 100.0))[0]

    assert default.rating == "poor"
    assert tolerant.rating == "excellent"
