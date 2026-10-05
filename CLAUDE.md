# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code
in this repository.

## Commands

### Development Setup

```bash
uv sync

# Run the MCP server (stdio transport, default)
uv run weather-mcp

# Run with HTTP transport
WEATHER_MCP_TRANSPORT=http uv run weather-mcp
```

### Testing

```bash
uv sync --extra test
uv run pytest
uv run pytest tests/test_comfort.py
uv run pytest -k "typical"
```

The suite is entirely offline. Nothing in `tests/` opens a socket, and no test
reaches Open-Meteo — `respx` intercepts at the transport layer.

### Linting

```bash
uvx ruff@0.16.4 check src tests
```

The rule set is narrow on purpose (`E9`, `F`, `B` — see `[tool.ruff.lint]` in
`pyproject.toml`): bugs, not style. Adding style rules would mean reformatting
files unrelated to whatever you are changing, so don't, unless asked for a sweep.

Pin the version here and in `.github/workflows/ci.yml` together; ruff adds rules
in minor releases and an unpinned linter turns `main` red on a morning nobody
touched anything.

### CI

`.github/workflows/ci.yml` runs on push to `main`, on every pull request, and
weekly. Three jobs: `test` (macOS and Linux × Python 3.12 and 3.13), `lint`, and
`audit` (`pip-audit` over the exported lockfile). The schedule exists for
`audit` — an advisory published today applies to a lockfile that has not changed
in months, so it has to run on a clock rather than only on a push.

## Architecture Overview

An MCP server that bridges a model to Open-Meteo. Read-only throughout: there is
no write path, no credential, and nothing here can change anything about the
world. That is why there is no equivalent of dav-mcp's `_last_write`
bookkeeping and no write check in `/health`.

1. **src/weather_mcp/openmeteo.py** — HTTP plumbing shared by all three hosts
   (forecast, geocoding, archive): parameter encoding, retries, the concurrency
   gate, and error mapping. Everything that can fail turns into a
   `WeatherError` carrying a message a model can act on.
2. **src/weather_mcp/geocoding.py** — place names to coordinates, the
   process-wide cache, and the home default. One-directional by necessity;
   there is no reverse lookup to build.
3. **src/weather_mcp/wmo.py** — WMO 4677 codes to plain English, plus the wet
   and severe subsets. The integer is never surfaced alone.
4. **src/weather_mcp/forecast.py** — request building, the date-window rules,
   and the column-array to row-dict transposition.
5. **src/weather_mcp/climate.py** — archive aggregation for
   `get_typical_weather`. Complete past years only.
6. **src/weather_mcp/comfort.py** — the day scoring behind `find_best_days`.
   Pure functions over hourly rows, so it is tested without a network or a clock.
7. **src/weather_mcp/server.py** — the tools, the `/health` route, and transport
   selection in `main()`.
8. **src/weather_mcp/auth.py** — password-guarded OAuth 2.1 provider for the
   HTTP transport. Ported from dav-mcp; domain-independent apart from the scope
   name, the env prefix, and the login page title. **Keep it that way**, so a
   fix in one project can be carried to the other by reading a diff.

## Key Implementation Details

- FastMCP 3.x provides the protocol. Tools are bare `@mcp.tool` on `async def`;
  the schema comes from the signature and the docstring, so the docstrings are
  written **at the model**, not at a developer.
- Tools return a `ToolResult` carrying both a human-readable text channel and
  `structured_content`. Errors return `_error_result`, never raise — a raised
  exception reaches the model as an opaque failure it cannot act on.
- Transport env vars are read inside `main()`, not at import time, so launchd
  and the tests can set them after import.
- `stateless_http` defaults to `True`. A remote client dials from a pool of
  addresses; a request arriving from a different address than the one that
  opened the session is rejected with a 400 and the connection wedges.

## Working with Open-Meteo

Verified against the live API. Each of these cost something before it was
understood, so check here before assuming the docs apply:

- **Errors arrive as `200 OK` about as often as `4xx`**, with
  `{"error": true, "reason": "..."}` in the body. `_body` therefore parses
  before it judges the status. The `reason` strings are unusually good and are
  passed to the model unedited — they name the parameter and the allowed range.
- **429 is a burst guard, not a spent budget.** Ten concurrent archive requests
  reliably get five rejected and the same five accepted a second later. Failing
  on the first 429 made `get_typical_weather` fail outright. It is retried, and
  `MAX_CONCURRENCY` caps in-flight requests at 3 so the situation is rarer.
- **Units are per family.** `temperature_unit=fahrenheit` does *not* make
  precipitation come back in inches. Every reply reads units back off the
  response rather than echoing the request.
- **An hourly response's `daily_units` block is a trap.** An hourly request also
  asks for sunrise and sunset, so `daily_units` exists and carries neither a
  temperature nor a precipitation key. `_units_of` merges both blocks; reading
  the first one that exists printed `59` and `0.07 ` where `59°F` and
  `0.07 inch` belonged.
- **The geocoder does not run backwards.** Passing `latitude`/`longitude` to it
  is rejected outright. Do not add a reverse lookup; there is nothing to call.
- **Pollen is Europe-only** — Berlin returns real counts, every US coordinate
  returns `null` for every hour. This is why the Air Quality API is not wrapped.
- **The archive is current to about yesterday**, not lagged by weeks.
  `get_typical_weather` still uses only complete past years, so a partly
  observed current year cannot skew an average.
- **`forecast_days` is capped at 16** and explicit dates are refused outside a
  moving window of about 92 days back and 16 forward.

## Never let history be read as a forecast

`get_typical_weather` is the one tool here that can mislead. It answers a
question that *sounds* like the forecast question, for dates where no forecast
exists, and a model that quotes "average high 62°F" without the frame has told
the user something false about next month.

Three defenses, all deliberate, none decorative:

- The reply's second line says "not a forecast" before any number appears.
- The structured payload carries `"kind": "history"`.
- The tool docstring and the server `instructions` both say it must not stand in
  for a forecast once the dates come within 16 days.

Do not quiet any of these. And do not add the Climate API as a "better" answer
for far-future dates — it returns downscaled CMIP6 climate *projections*,
numbers that read exactly like a forecast and are not one.

## The comfort score is a preference, not a measurement

`comfort.py` ranks days. It is not physics and must not be dressed up as it.
Three named penalties, each with a stated calibration, and every reply reports
the components alongside the verdict.

Two things there are load-bearing:

- **The heat penalty is quadratic, and averaged per hour before it is summed.**
  With a linear penalty a real Austin day running 87°F to 104°F feels-like
  scored 79 and came back "good for a hike": ten pleasant morning degrees
  averaged out a dangerous afternoon. Squaring first drops it to 16.
- **`HEAT_DIVISOR` is 3, not 4.** At 4 a sustained 88–94°F afternoon still came
  back "fair". Both calibration points are pinned in `tests/test_comfort.py`;
  change the constant and the tests will say what you changed.

Rain uses the *peak* hour rather than the mean, because one hour at 80% is a
soaking that a dry morning should not average away. Temperature does the
opposite, and averages, because one uncomfortable hour in ten does not make a
day unusable. That asymmetry is intentional.

## Always name the place

Every tool resolves a place from free text, and every reply names what it
resolved to plus the other candidates. This is the only thing about a weather
reply that can be *silently* wrong: "Springfield" is five real cities, and a
forecast for the wrong one looks exactly like a forecast for the right one.
There is no signal in the numbers.

`WEATHER_MCP_HOME` is coordinates rather than a name for the same reason. A name
would be re-resolved on every call, so a shift in how Open-Meteo ranks "Austin"
could move home from Texas to Minnesota without anything appearing to change.

## Scope

Four data sources are wrapped: forecast, geocoding, archive, and nothing else.

Deliberately not built:

- **Air quality.** The US half of it is AQI and PM2.5 only; pollen, which is the
  part people actually ask about, returns `null` everywhere outside Europe.
- **Marine, flood, satellite radiation, ensemble.** No use case here.
- **Reverse geocoding.** Not offered by the API at any price.
- **Severe weather alerts.** Open-Meteo does not publish government warnings.
  A model must not infer "no alerts" from a calm forecast, which is why nothing
  here claims to cover them.
