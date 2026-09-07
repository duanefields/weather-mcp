"""Payloads shaped exactly the way Open-Meteo returns them.

The column-array layout is the point. A fixture written as a list of day dicts
would test a transposition that never happens, and would have hidden the bug
where an hourly response's ``daily_units`` block carries no temperature key.
"""

GEOCODE_BOSTON = {
    "results": [
        {
            "id": 4930956,
            "name": "Boston",
            "latitude": 42.35843,
            "longitude": -71.05977,
            "elevation": 14.0,
            "timezone": "America/New_York",
            "population": 653833,
            "country_code": "US",
            "country": "United States",
            "admin1": "Massachusetts",
            "admin2": "Suffolk",
        },
        {
            "id": 5109797,
            "name": "Boston",
            "latitude": 42.62895,
            "longitude": -78.73753,
            "timezone": "America/New_York",
            "population": 8049,
            "country": "United States",
            "admin1": "New York",
        },
    ]
}

# A name that matches nothing omits `results` rather than returning an empty list.
GEOCODE_EMPTY: dict = {"generationtime_ms": 0.4}

FORECAST_DAILY = {
    "latitude": 42.365166,
    "longitude": -71.0618,
    "utc_offset_seconds": -14400,
    "timezone": "America/New_York",
    "elevation": 19.0,
    "current_units": {
        "time": "iso8601",
        "temperature_2m": "°F",
        "weather_code": "wmo code",
        "wind_speed_10m": "mp/h",
    },
    "current": {
        "time": "2026-09-07T15:15",
        "temperature_2m": 81.2,
        "apparent_temperature": 76.4,
        "relative_humidity_2m": 40,
        "precipitation": 0.0,
        "weather_code": 0,
        "wind_speed_10m": 11.3,
    },
    "daily_units": {
        "time": "iso8601",
        "weather_code": "wmo code",
        "temperature_2m_max": "°F",
        "temperature_2m_min": "°F",
        "precipitation_sum": "inch",
        "precipitation_probability_max": "%",
        "wind_speed_10m_max": "mp/h",
    },
    "daily": {
        "time": ["2026-09-07", "2026-09-08", "2026-09-09"],
        "weather_code": [0, 3, 61],
        "temperature_2m_max": [81.4, 80.3, 75.0],
        "temperature_2m_min": [52.7, 59.8, 63.1],
        "apparent_temperature_max": [79.0, 78.0, 74.0],
        "apparent_temperature_min": [50.0, 57.0, 61.0],
        "precipitation_sum": [0.0, 0.0, 0.35],
        "precipitation_probability_max": [0, 12, 71],
        "precipitation_hours": [0.0, 0.0, 6.0],
        "wind_speed_10m_max": [11.0, 9.4, 14.2],
        "wind_gusts_10m_max": [20.1, 17.0, 28.5],
        "sunrise": ["2026-09-07T06:16", "2026-09-08T06:17", "2026-09-09T06:18"],
        "sunset": ["2026-09-07T19:12", "2026-09-08T19:10", "2026-09-09T19:09"],
        "uv_index_max": [6.5, 5.9, 3.2],
    },
}

# An hourly request asks for sunrise/sunset too, so `daily_units` exists and
# carries neither a temperature nor a precipitation key. Reading units from that
# block alone left every reading unlabeled.
FORECAST_HOURLY = {
    "timezone": "Europe/Amsterdam",
    "hourly_units": {
        "time": "iso8601",
        "temperature_2m": "°F",
        "precipitation": "inch",
        "precipitation_probability": "%",
        "wind_speed_10m": "mp/h",
    },
    "hourly": {
        "time": [f"2026-09-09T{hour:02d}:00" for hour in range(6)],
        "temperature_2m": [59.0, 58.5, 58.0, 60.1, 60.4, 61.0],
        "apparent_temperature": [57.0, 56.5, 56.0, 58.0, 58.4, 59.0],
        "precipitation_probability": [10, 12, 45, 68, 86, 5],
        "precipitation": [0.0, 0.0, 0.02, 0.13, 0.04, 0.0],
        "weather_code": [0, 2, 53, 63, 53, 1],
        "wind_speed_10m": [7.0, 7.2, 8.1, 9.0, 8.8, 6.0],
        "relative_humidity_2m": [80, 82, 88, 92, 90, 78],
    },
    "daily_units": {"time": "iso8601", "sunrise": "iso8601", "sunset": "iso8601"},
    "daily": {
        "time": ["2026-09-09"],
        "sunrise": ["2026-09-09T07:05"],
        "sunset": ["2026-09-09T20:10"],
        "weather_code": [63],
    },
}


def archive_year(highs, lows, rain):
    """One year's window out of the archive, in the API's own shape."""
    return {
        "timezone": "America/New_York",
        "daily_units": {
            "time": "iso8601",
            "temperature_2m_max": "°F",
            "temperature_2m_min": "°F",
            "precipitation_sum": "inch",
        },
        "daily": {
            "time": [f"2020-10-{14 + index:02d}" for index in range(len(highs))],
            "temperature_2m_max": highs,
            "temperature_2m_min": lows,
            "precipitation_sum": rain,
        },
    }
