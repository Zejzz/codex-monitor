# Codex Monitor

A local dashboard for Codex chat and agent usage. Runs with Python's standard library and a browser; no API key or package installation is required.

## Features

- Running chats first, archived chats, and historical subagent trees.
- Token usage grouped by the model and effort recorded for each request, including changes within a chat.
- API-equivalent cost estimates for input, cached input and output tokens.
- Requested speed-tier coverage, configurable missing-tier assumptions, and a comparison between reported cache hits and modeled cache-switch rules.
- Per-model search, date ranges, daily/weekly/monthly charts and cumulative totals.
- Turn timing and the latest recorded weekly account-limit snapshot.
- Incremental persistent caches and optional authenticated collectors for other devices.
- Responsive layout for desktop and mobile browsers.

## Run locally

Requires Python 3.10 or later and local Codex records.

```sh
python monitor.py
```

Open **http://127.0.0.1:8765**. On systems where Python is named `python3`, use that command instead.

The default data directory is `~/.codex`. Select another directory with `--home PATH` or `CODEX_HOME`. Change the dashboard port with `--port PORT`.

On Windows, double-click **Open Codex Monitor.cmd**. To start quietly at sign-in, run `enable-autostart.ps1`; it creates a shortcut in the current user's Startup folder. Run **Stop Codex Monitor.cmd** to stop the backend.

The first scan builds a cache. Later scans read appended data and check changed files. Restarts reuse saved telemetry before the background scan catches up. The UI polls every five seconds.

## Multiple devices

Each device needs its own Codex records and collector. Signing into the same account elsewhere does not automatically provide those records.

On the receiving computer, with Tailscale running:

```sh
python setup_remote.py --name "Remote device"
```

This creates a private paired collector folder and ZIP in `collectors/`. Transfer the complete folder securely to the sending computer. Do not publish it: it contains a device-specific upload key.

On the sending computer:

```sh
python collector.py
```

Windows bundles also include **Start Collector.cmd**. On Linux, `bash install-linux-collector.sh` installs a persistent user service; enabling linger may require administrator access.

Restart the receiver after first-time setup. Later paired devices do not require a restart. The receiver validates each device's bearer key, accepts loopback or Tailscale IPv4 binds, and rejects browser-origin uploads. A restarted receiver keeps uploaded history in its SQLite database. An unavailable device's saved history remains visible.

For a dedicated receiver without local Codex history, run:

```sh
python monitor.py --remote-only
```

The dashboard itself binds to loopback. Remote UI access requires your own private network or authenticated gateway configuration; none is installed by this repository. If forwarding a different Host header, explicitly configure `dashboard-access.json` with a `hosts` array of permitted `hostname:port` values. Protect every UI/API route; allowlisting a Host header is not authentication. Preserve same-origin checks on pricing writes. Keep the receiver and dashboard off the public internet.

## Privacy and stored data

The repository contains source code and synthetic tests. It includes no session histories, usage databases, deployment credentials, paired collector bundles or production configuration.

At runtime, **usage metadata is sensitive**. The dashboard and optional collectors expose chat titles, project paths, identifiers, agent/model/effort history, timestamps, token counts and quota observations. Keep the dashboard private. Runtime caches contain metadata; remote receiver settings and paired bundles contain credentials.

The monitor reads Codex's local session files and SQLite databases to extract telemetry. It does not modify Codex records or read Codex authentication files. Message bodies and tool outputs are not included in collector exports or dashboard responses. Chat titles themselves may contain text from a prompt, so metadata is not anonymous. Optional remote collection sends metadata to the endpoint you configure, not to a service operated by this project.

See [PRIVACY.md](PRIVACY.md) for publication and runtime privacy details.

## Estimates and limitations

- API equivalents are estimates, not subscription charges or invoices. Bundled rates are a dated configuration snapshot; verify and update them before relying on costs. Edit `prices.json` or use the API rates dialog. Unpriced models are reported separately.
- Usage follows the recorded model for each request. Historical aliases and Auto-review proxy rates are configurable assumptions, not verified model-resolution or billing records.
- Requested speed tiers drive estimates. Missing tier evidence remains unknown; Standard is assumed unless the Fast scenario is selected. Delivered tier and subscription quota consumption cannot be reconstructed reliably from these records.
- The optional cache-switch scenario models a cold first request on model changes, and on effort changes for older generations. It also considers parent/subagent boundaries. It does not alter recorded tokens. Reported-cache mode uses recorded cache hits directly.
- Long-context multipliers apply per record using the configured threshold. Legacy cumulative deltas may cover multiple requests and reduce accuracy. Reasoning tokens are already part of output. Tool fees and other surcharges are excluded.
- Modern copied agent history is excluded when ownership markers are available. Older records can lack information needed for exact attribution.
- The weekly meter is the latest recorded account snapshot. Per-chat percentages allocate it by observed token share; they do not reproduce server-side model weighting. Account switching cannot be reconstructed reliably, so this is not an account-separated billing ledger.
- Activity status is inferred from recorded turns and collector freshness. Old unfinished turns become unconfirmed. Parallel agent durations are separate wall-time observations.
- Local Codex schemas can change. Missing or rotated historical records cannot be recreated by the monitor.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests use temporary directories, synthetic records and loopback endpoints. They cover token ownership, historical model/effort attribution, cache rules, startup caching and authenticated remote ingestion.
