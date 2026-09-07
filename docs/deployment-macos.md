# Running as a background service on macOS

How to host the HTTP server on an unattended Mac and reach it from a Claude
connector over a Cloudflare Tunnel.

This is the sibling of dav-mcp's `docs/deployment-macos.md`, and most of it is
the same. The differences are worth stating up front, because this is the
easiest of the family to run.

## What this server does not need

It talks to `api.open-meteo.com` over HTTPS and touches no local application, no
protected container and no Apple Events. So there is no Full Disk Access grant
to make, no Automation prompt waiting to ambush a first request, and no GUI
session required — it *could* be a LaunchDaemon.

More to the point: **there is no credential at all.** Open-Meteo's free tier
needs no API key for non-commercial use. The one secret in the plist below is
`WEATHER_MCP_PASSWORD`, which guards this server rather than an account
somewhere else. Nothing here can be revoked out from under you, and nothing
here, if leaked, grants access to anything but public weather data and this
host's share of a free API.

Run it as a **LaunchAgent anyway** if the host already supervises other services
that way and already logs in automatically — one mechanism then covers all of
them. `/health` publishes `python_version` for the same reason its siblings do:
it costs nothing and keeps the monitoring identical across services.

## Invoke the interpreter directly, not `uv run`

`uv run` spawns the interpreter as a child, so launchd ends up supervising the
wrapper. Killing the job leaves the real server holding the port. Point
`ProgramArguments` at the venv's interpreter.

## Template

Save as `~/Library/LaunchAgents/com.example.weather-mcp.plist`, replacing the
placeholders. It contains one secret, so `chmod 600` it.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.weather-mcp</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/USERNAME/Code/weather-mcp/.venv/bin/python</string>
    <string>-m</string>
    <string>weather_mcp</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/USERNAME/Code/weather-mcp</string>

  <key>EnvironmentVariables</key>
  <dict>
    <!-- Coordinates, not a name: a name would be re-resolved on every call. -->
    <key>WEATHER_MCP_HOME</key><string>40.7128,-74.0060</string>
    <key>WEATHER_MCP_HOME_NAME</key><string>New York</string>

    <key>WEATHER_MCP_TRANSPORT</key><string>http</string>
    <key>WEATHER_MCP_HOST</key><string>127.0.0.1</string>
    <key>WEATHER_MCP_PORT</key><string>18791</string>
    <key>WEATHER_MCP_AUTH</key><string>password</string>
    <key>WEATHER_MCP_PASSWORD</key><string>REPLACE-WITH-A-LONG-RANDOM-VALUE</string>
    <key>WEATHER_MCP_BASE_URL</key><string>https://weather.example.com</string>
  </dict>

  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/USERNAME/.weather-mcp/server.log</string>
  <key>StandardErrorPath</key><string>/Users/USERNAME/.weather-mcp/server.log</string>
</dict>
</plist>
```

```bash
chmod 600 ~/Library/LaunchAgents/com.example.weather-mcp.plist
launchctl load ~/Library/LaunchAgents/com.example.weather-mcp.plist
curl -s http://127.0.0.1:18791/health
```

## Ingress

Bind to loopback and put a Cloudflare Tunnel in front. Add an ingress entry
alongside whatever else the host already serves rather than starting a second
tunnel:

```yaml
ingress:
  - hostname: weather.example.com
    service: http://127.0.0.1:18791
  - service: http_status:404
```

**Watch the YAML.** A `service:` must be a key of the same list item as its
`hostname`, not a new `- ` entry. Written as two entries, the stray
`- service: ...` becomes a catch-all matching every hostname and
`cloudflared tunnel ingress validate` fails — which means the tunnel refuses to
start on next boot and takes *every* hostname on it down with it. A running
tunnel keeps serving its old in-memory config, so the breakage stays invisible
until a restart. Validate after every edit:

```bash
cloudflared --config ~/.cloudflared/config.yml tunnel ingress validate
```

**No Cloudflare Access application in front of the hostname.** Access intercepts
the OAuth callbacks and breaks the connector handshake.

## Monitoring

`scripts/healthcheck.sh` polls `/health` and reports to a dead-man's-switch;
`scripts/self-update.sh` pulls the tracked branch and restarts the service when
it moves. Both read config from `~/.weather-mcp/`, and both files must be
`chmod 600` — a ping URL is a capability, not just an address.

```
*/10 * * * * /Users/USERNAME/Code/weather-mcp/scripts/healthcheck.sh >> /Users/USERNAME/.weather-mcp/check.log 2>&1
*/15 * * * * /Users/USERNAME/Code/weather-mcp/scripts/self-update.sh >> /Users/USERNAME/.weather-mcp/update.log 2>&1
```

Use a **distinct** healthchecks.io UUID per service. Sharing one lets a live
service's pings mask a dead one's silence, which is the exact failure the
monitoring exists to catch.

The health check watches three things: whether the process answers at all,
whether Open-Meteo is reachable, and whether a home location is configured. That
last one is not fatal — every tool still works when a place is named explicitly
— but without it every question about "here" fails, which is most of them.

## Cutover order

The issuer is baked into the connector's registration, so:

1. Set the final `WEATHER_MCP_BASE_URL`.
2. Start HTTP mode on the host.
3. Verify over the tunnel from off-network.
4. Add the connector in Claude and authorize once.

MCP Inspector passing is not sufficient evidence — test against Claude directly.
