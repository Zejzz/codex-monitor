# Privacy

## Published files

Only application source, launch/install scripts, rate configuration, documentation and synthetic tests are intended for publication. Local deployments and their Git histories are not part of this source release.

Do not commit runtime databases, session files, screenshots of real dashboards, exported reports, machine/account identifiers, private hostnames, endpoint configuration, paired bundles, logs or credentials. The ignore rules reduce accidental additions; inspect the actual Git index and history before pushing.

## Runtime metadata

Titles, workspace paths, model usage, timestamps and account-limit observations may reveal personal information even without message bodies. Treat caches, dashboard responses and collector exports as private. Protect the complete dashboard and API if using a gateway.

Only pair trusted devices. Each collector bundle contains an upload key. Remove a device's entry from the receiver's private `remote-config.json` to revoke that device's future uploads; this does not remove previously stored history. Do not paste configurations, usage snapshots or logs into public issues.

The application does not send telemetry to its maintainers. A collector sends selected usage metadata to its configured receiver. Read the source and configure your own network access before enabling remote collection.

## Reporting a problem

For public bug reports, use synthetic reproductions. Remove titles, paths, device names, identifiers and credentials from any example. For a vulnerability that could expose data, use GitHub's private vulnerability reporting for this repository if available; do not post live secrets in an issue.
