# weather-mcp

[![CI](https://github.com/duanefields/weather-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/duanefields/weather-mcp/actions/workflows/ci.yml)

An MCP server for weather, over [Open-Meteo](https://open-meteo.com). Forecasts
anywhere in the world, hour by hour when the day matters, and observed history
for the trips that are still too far out to forecast.

No API key. Open-Meteo's free tier needs no credential at all for
non-commercial use, so there is nothing to store and nothing to leak. It runs
over stdio locally, or over authenticated HTTP for a remote client such as a
Claude connector.

Built for one question in particular: *I have a trip — what should I expect?*

## Tools

| Tool | What it does |
| :--- | :--- |
| `search_locations` | A name → the places it could mean, with coordinates, region and timezone. The disambiguator. |
| `get_forecast` | Daily outlook for a place and date range: highs, lows, rain chance, wind. Up to 16 days. |
| `get_hourly_forecast` | One day, hour by hour, with contiguous rain windows called out. |
| `get_typical_weather` | What the weather actually did on these dates in past years. History, for trips past the horizon. |
| `find_best_days` | Ranks the days ahead by how pleasant it will be outside. For picking a day to hike. |

`GET /health` is served unauthenticated alongside them, for monitoring.

## The three things worth knowing

### Places are ambiguous and forecasts are not self-checking

"Springfield" is five real US cities. "Portland" is two large ones. A forecast
for the wrong one looks exactly like a forecast for the right one — there is no
signal in the numbers that anything went wrong.

So every tool takes a place as ordinary text, resolves it, and **names the place
it resolved to in every reply**, along with the other candidates it matched:

```
Forecast for Boston, Massachusetts, United States (also matched: Boston,
New York, United States; Boston, Georgia, United States)
```

`search_locations` exists for the cases where that is not enough and the model
should ask. It is not a required first step.

### The horizon is 16 days, and that is the data, not a setting

`api.open-meteo.com` refuses anything further out:

```
{"reason":"Parameter 'start_date' is out of allowed range from
 2026-06-06 to 2026-09-22","error":true}
```

Which is a problem, because trips get planned months ahead and the weather
question arrives with the flights. `get_typical_weather` answers that case from
the Historical Weather API — the same calendar window in each of the last ten
complete years, as observed:

```
Typical weather for 10-14 to 10-18 in Boston, Massachusetts, United States
This is what actually happened in 2016–2025 (10 years), not a forecast.

- Average high 64.2°F, average low 48.1°F
- Warmest 79.3°F, coldest 36.9°F
- Rain during this window in 7 of 10 years
```

This is history and the tools say so in the first line, in the docstrings, and
in the structured payload (`"kind": "history"`). It answers *what to pack*, not
*what will happen*.

The Climate API would answer far-future dates directly, and is deliberately not
used: it returns downscaled CMIP6 climate **projections**, numbers that read
exactly like a forecast and are not one.

### Timezones belong to the destination, not the server

A forecast for "the day of" is meaningless without a local day boundary, and the
relevant boundary is the destination's. This matters most for the case the
server was built for: asking about a destination from a laptop that is somewhere
else entirely, which is the normal state of affairs when the question is about a
trip.

Every request goes out with `timezone=auto`, so the API anchors the days to the
place being asked about. When no dates are given, the range is expressed as a
day *count* rather than computed dates, so "the next week" starts on today at
the destination. The server's own clock is never consulted for this.

### Picking a day is not the same as reading a forecast

"What's a good day for a hike in the next two weeks" is arithmetic over three
hundred hourly readings, and doing it by eye off a daily forecast goes wrong in
two specific ways. `find_best_days` exists for it, and makes two decisions:

**It scores the hours you would actually be outside** — 8am to 6pm by default,
adjustable — rather than the whole day. A daily high is one afternoon moment,
and a daily rain chance includes the hours you were asleep.

**It scores feels-like, not temperature.** Open-Meteo's `apparent_temperature`
already folds in humidity, wind chill and sun, which is exactly the difference
between 85°F in Austin and 85°F in Denver. Reconstructing that from dry-bulb
temperature plus a humidity correction would be rebuilding, worse, something the
API already did.

The penalty for heat is quadratic, which is not decoration. With a linear one, a
real Austin day running 87°F to 104°F feels-like scored 79 and came back "good
for a hike" — ten pleasant morning degrees had averaged out a dangerous
afternoon. Squaring the deviation before averaging drops it to 16, which is the
right answer. Both calibration points are pinned in `tests/test_comfort.py`.

The ranking is a stated preference, not a measurement, so every day comes back
with the numbers behind it and days scored *poor* are named as days to avoid
rather than padding out a top five:

```
Best days to be outside in Austin
Scored over 08:00–18:00 America/Chicago, comfortable between 50°F and 78°F feels-like.

1. 2026-09-18 — excellent (89/100)
   feels like 72°F–86°F, overcast, 11% chance of rain, wind to 8 mph, 35% humidity
2. 2026-09-17 — good (82/100)
   feels like 79°F–89°F, overcast, 10% chance of rain (0.03 inch), wind to 13 mph, 64% humidity

Worth avoiding: 2026-09-07 (feels like 104°F), 2026-09-08 (feels like 105°F)
```

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env      # then set your home location
```

```bash
uv run weather-mcp                                 # stdio
WEATHER_MCP_TRANSPORT=http uv run weather-mcp      # http on 127.0.0.1:18791
```

To use it from Claude Code over stdio:

```bash
claude mcp add weather -- uv --directory /path/to/weather-mcp run weather-mcp
```

## Configuration

Everything is environment variables; nothing is read from `.env` by the server
itself, which only documents them.

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `WEATHER_MCP_HOME` | — | Home location as `latitude,longitude`. Used when a tool is called with no place. |
| `WEATHER_MCP_HOME_NAME` | `Home` | What to call it in replies. |
| `WEATHER_MCP_HOME_TIMEZONE` | resolved | IANA zone for the home label. Optional; forecasts resolve it from the coordinates regardless. |
| `WEATHER_MCP_TEMPERATURE_UNIT` | `fahrenheit` | `fahrenheit` or `celsius`. |
| `WEATHER_MCP_WIND_SPEED_UNIT` | `mph` | `mph`, `kmh`, `ms` or `kn`. |
| `WEATHER_MCP_PRECIPITATION_UNIT` | `inch` | `inch` or `mm`. |
| `WEATHER_MCP_TRANSPORT` | `stdio` | `stdio` or `http`. |
| `WEATHER_MCP_HOST` | `127.0.0.1` | HTTP bind address. |
| `WEATHER_MCP_PORT` | `18791` | HTTP bind port. |
| `WEATHER_MCP_STATELESS` | `true` | `false` restores per-client sessions. |
| `WEATHER_MCP_AUTH` | `none` | `none` or `password`. HTTP only. |
| `WEATHER_MCP_PASSWORD` | — | Shared password. Required when `AUTH=password`. |
| `WEATHER_MCP_BASE_URL` | — | Public URL; becomes the OAuth issuer. Required when `AUTH=password`. |
| `WEATHER_MCP_STATE_DIR` | `~/.weather-mcp` | Where OAuth state is persisted. |

**`WEATHER_MCP_HOME` is coordinates, not a name.** A name would be re-geocoded
on every call, and re-*resolved* — so a shift in how Open-Meteo ranks "Austin"
could quietly move home from Texas to Minnesota. Run `search_locations` once and
paste the numbers.

## Authentication

A remote MCP client has one input field: a URL. There is nowhere to put an API
key. So `WEATHER_MCP_AUTH=password` starts a self-contained OAuth 2.1
authorization server whose only credential is one shared password — the client
discovers it, registers itself, and gets redirected to a password form.

Dynamic client registration, PKCE, discovery metadata and the `401` challenge
come from FastMCP and the MCP SDK. This project adds the login screen and the
credential check. Registered clients and tokens persist across restarts;
authorization codes and in-flight logins are deliberately memory-only.

The data served is public weather, so an open port leaks nothing about the
operator. What it does hand out is the **call budget** this server runs on —
10,000 requests a day, shared with every real tool call, on a tier with no
uptime guarantee. That is the reason to authenticate.

Bind to localhost and put a tunnel or reverse proxy in front of it. See
[docs/deployment-macos.md](docs/deployment-macos.md).

## Notes on Open-Meteo

Findings that shaped the implementation, each verified against the live API:

- **Geocoding is included**, at `geocoding-api.open-meteo.com`, on the same
  free terms. A separate geocoding service is not needed. It returns the IANA
  timezone alongside the coordinates, which is the part that makes local day
  boundaries correct.
- **There is no reverse geocoding.** Passing `latitude`/`longitude` to the
  geocoding endpoint is rejected outright. Coordinates → place name is not
  available at any price, so this server never attempts it.
- **Errors arrive as `200 OK` about as often as `4xx`**, with
  `{"error": true, "reason": "..."}` in the body. The body is therefore parsed
  before the status is judged. The `reason` strings are unusually good and are
  passed through to the model unedited — they say exactly which parameter was
  wrong and what range was allowed.
- **Units are per family.** `temperature_unit=fahrenheit` does *not* make
  precipitation come back in inches; it stays in millimeters until
  `precipitation_unit` is set too. Replies read the units back off the response
  rather than echoing the request, so a number can never be labeled with a unit
  it is not in.
- **Pollen is Europe-only.** Berlin returns real grass and birch counts; every
  US coordinate returns `null` for every hour. This is why the Air Quality API
  is not wrapped — the half of it that works in the US is not worth a tool.
- **The archive is current to about yesterday**, not lagged by weeks as the
  "historical" framing suggests. `get_typical_weather` still uses only complete
  past years, so a partly-observed current year can never skew an average.

## Development

```bash
uv sync --extra test
uv run pytest
uvx ruff@0.16.4 check src tests
```

The suite is entirely offline — no test reaches Open-Meteo. Fixtures reproduce
the exact payload shapes the API returns, including its column-array layout.

## License

MIT
