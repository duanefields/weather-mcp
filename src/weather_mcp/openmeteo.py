"""HTTP plumbing shared by every Open-Meteo endpoint.

Open-Meteo is several hosts (forecast, geocoding, archive) speaking the same
dialect: query parameters in, JSON out, and errors reported as ``200 OK`` with
``{"error": true, "reason": "..."}`` in the body as often as with a 4xx. So the
one thing this module exists to do is turn every shape of failure into a
``WeatherError`` carrying a message a model can act on.

There are no credentials. The free tier needs no API key, which removes the
failure mode that dominates dav-mcp's monitoring -- a revoked password with no
local symptom -- and replaces it with a call budget: 10,000/day, 5,000/hour,
600/minute, non-commercial use only.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

TIMEOUT_SECONDS = 20.0

# Two retries on a transient failure, then give up. Kept short for the same
# reason dav-mcp keeps its retry budget under ~10s: a tool call that hangs for
# a minute is worse than one that fails and lets the caller try again.
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 1.0

# Open-Meteo answers a burst of concurrent requests with 429 well before the
# documented per-minute ceiling is anywhere near spent -- ten parallel archive
# requests reliably get five of them rejected, and the same five succeed a
# second later. So 429 is retried like any other transient failure, with a
# longer pause, and only becomes an error after the retries are used up.
RATE_LIMIT_BACKOFF_SECONDS = 2.0

# In-flight requests, process-wide. Cheaper than discovering the burst limit by
# tripping it: `climate.typical` would otherwise fire one request per year all
# at once and spend most of its wall time on retries.
MAX_CONCURRENCY = 3


class WeatherError(Exception):
    """Anything that stopped a request from producing usable data."""


class RateLimited(WeatherError):
    """The free tier's call budget is exhausted.

    Distinguished from a generic failure because the remedy is entirely
    different: nothing is broken, and waiting fixes it.
    """


_client: httpx.AsyncClient | None = None
_gate = asyncio.Semaphore(MAX_CONCURRENCY)


def _http() -> httpx.AsyncClient:
    """The process-wide HTTP client.

    One client, so connections to the three Open-Meteo hosts are pooled across
    tool calls rather than reopened per request.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": "weather-mcp (+https://github.com/duanefields/weather-mcp)"},
            follow_redirects=True,
        )
    return _client


async def get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch one Open-Meteo endpoint and return its parsed body.

    Parameters whose value is ``None`` are dropped, so callers can build a
    parameter dict with optional entries without pruning it themselves. Lists
    are joined with commas, which is how Open-Meteo expects the ``daily=`` and
    ``hourly=`` variable selections.
    """
    query: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            if not value:
                continue
            query[key] = ",".join(str(item) for item in value)
        elif isinstance(value, bool):
            query[key] = "true" if value else "false"
        else:
            query[key] = str(value)

    last: Exception | None = None
    pause = BACKOFF_SECONDS
    for attempt in range(MAX_ATTEMPTS):
        pause = BACKOFF_SECONDS
        try:
            async with _gate:
                response = await _http().get(url, params=query)
        except httpx.HTTPError as exc:
            # Network-level: DNS, connect, read timeout. Worth retrying.
            last = exc
            logger.warning("Open-Meteo request failed (%s), attempt %d", exc, attempt + 1)
        else:
            if response.status_code == 429:
                last = RateLimited(
                    "Open-Meteo refused the request as too frequent, and kept refusing it. "
                    "Nothing is broken; wait a moment and ask again."
                )
                pause = RATE_LIMIT_BACKOFF_SECONDS
                logger.warning("Open-Meteo rate limited, attempt %d", attempt + 1)
            # 5xx is the upstream having a moment; 4xx other than 429 is this
            # server's own bad request and will fail identically on a retry.
            elif response.status_code >= 500:
                last = WeatherError(f"Open-Meteo returned {response.status_code}.")
                logger.warning("Open-Meteo returned %d, attempt %d", response.status_code, attempt + 1)
            else:
                return _body(response)

        if attempt < MAX_ATTEMPTS - 1:
            await asyncio.sleep(pause * (attempt + 1))

    if isinstance(last, RateLimited):
        raise last
    raise WeatherError(f"Could not reach Open-Meteo: {last}")


def _body(response: httpx.Response) -> dict[str, Any]:
    """Parse a response, honoring Open-Meteo's in-band error reporting.

    An invalid parameter comes back as a 400 whose body carries the useful part,
    so the body is parsed before the status is judged. ``reason`` is genuinely
    good prose -- "Parameter 'start_date' is out of allowed range from
    2026-06-06 to 2026-09-22" -- and passing it through unedited tells the model
    exactly how to fix its own call.
    """
    try:
        payload = response.json()
    except ValueError:
        raise WeatherError(
            f"Open-Meteo returned {response.status_code} with a body that is not JSON."
        ) from None

    if isinstance(payload, dict) and payload.get("error"):
        raise WeatherError(str(payload.get("reason", "Open-Meteo rejected the request.")))

    if response.status_code >= 400:
        raise WeatherError(f"Open-Meteo returned {response.status_code}.")

    if not isinstance(payload, dict):
        raise WeatherError("Open-Meteo returned an unexpected payload.")

    return payload


async def aclose() -> None:
    """Close the shared client. For tests; the server runs until killed."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
