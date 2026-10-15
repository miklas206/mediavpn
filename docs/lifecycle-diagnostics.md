# Persistent lifecycle diagnostics

MediaVPN 0.2.13 automatically initializes `lifecycle.sqlite` in its existing data
directory on fresh installs and upgrades. It retains 2,000 safe structured events
across restart/container replacement; identical error/blocked observations are
limited to once per minute. No new package, mount, device or capability is needed.

Administrators can read the latest 200 entries at `GET /api/diagnostics/logs`.
MediaHub uses `GET /api/integration/diagnostics/logs` with the existing management
key; keys and raw Docker logs are never returned. Preview mode keeps diagnostics
available but starts no Docker observer or VPN tunnel.

Events cover authenticated lifecycle/settings actions, automatic recovery,
provider contract mismatches, public IP/port changes, selected server, verification
stages, torrent/gateway safety stops and owner-scoped Docker lifecycle/health
events. Controller operation IDs and provider/container generations correlate
causes and actions. Exceptions retain type/code/status/source location only.
Docker die events retain exit/signal/OOM evidence even after removal. Docker exec
events, configurations, private keys, request bodies and exception text are excluded.

The observer logs its observation window and connection interruptions. Docker
does not report callers of arbitrary external commands; those events explicitly
say `docker_event_caller_not_reported`. Power/process/storage failure, observer
downtime and retention limits can leave gaps. Diagnostic write failure is reported
and never prevents a safety stop. A new journal cannot reconstruct older incidents.
