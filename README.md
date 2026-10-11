# MediaVPN

Independent MediaHub-family VPN profile manager, using FjordVPN as a functional reference, not copied application source. MediaHub Core/Agent and existing VPN installations are not required or modified.

## Current Scope

- Local administrator setup/login, session cookies, CSRF and same-origin writes.
- Multiple WireGuard profiles, bounded file upload, duplicate-key rejection, start/stop/remove.
- Full IPv4 tunnel, public IP endpoint and a restricted configuration grammar; shell hooks and arbitrary directives rejected.
- Per-profile Gluetun container with firewall enabled, no published ports, only NET_ADMIN and TUN device.
- Status requires a running container and a successful Docker healthcheck no older than 90 seconds. This is tunnel health evidence, NOT independent public egress or leak-test proof.
- Standard preview saves profiles but cannot start VPNs. The privileged runtime must be deliberately enabled.

**Not yet implemented:** Proton NAT-PMP port forwarding, public egress IP verification, TCP relay, MediaSeed traffic attachment, hub SSO, automatic app installation/update and live VPN credentials testing. Existing app traffic is NOT protected by merely creating or starting a profile. IPv6 leak protection and attached-app fail-closed behavior remain unverified: do not route downloads through this release.

## Preview Installation

```sh
docker compose up -d --build
```

Open http://127.0.0.1:18802 and create a local administrator. For a trusted LAN, configure MEDIAVPN_BIND and matching MEDIAVPN_BASE_URL. Use HTTPS before uploading real VPN keys over an untrusted network. No VPN or Docker-host control is enabled by this command.

The named appdata volume contains accounts and profile keys. Never delete it to upgrade. WireGuard keys are plaintext in owner-only files (0600), not encrypted at rest; the database is also owner-only. Protect the host and backups. Do not submit real keys to a preview deployed over plain HTTP.

## Opt-In Runtime

The runtime overlay grants the web process access to the Docker socket and runs it as root. This is effectively Docker-host control, despite runtime code only selecting MediaVPN-labelled containers. It is not a security sandbox. Enable only on a dedicated, trusted app host with HTTPS, protected backups and /dev/net/tun already configured.

Provision a separate protected Linux directory, e.g. /opt/mediavpn-data (0700). Set MEDIAVPN_HOST_DATA to its absolute path and pre-pull the configured Gluetun image. The host path must match the app's /data bind mount: do not use a remote Docker daemon. Runtime overlay switches away from preview's named volume; it does not migrate preview accounts or keys automatically.

```sh
docker pull qmcgaw/gluetun:v3.40.0
docker compose -f compose.yaml -f compose.runtime.yaml config --quiet
docker compose -f compose.yaml -f compose.runtime.yaml up -d --build
```

Each new profile is saved stopped. Start only through its explicit Start action. The container remains isolated; no relay or app attachment is created. Stopping removes tunnel connectivity, not existing application containers. Removing a profile first stops/removes its owned tunnel; failures preserve the key. Profile keys cannot be downloaded through the API.

## Tests And Upgrade

```sh
python -m pytest tests -q
```

Tests use synthetic keys and fake runtime objects. No live VPN access or traffic is involved. UI tests require Playwright and Chrome.

Upgrade with a new app image and the same data mount. Keep the previous image tag for rollback; no automatic destructive data migration. Preserve running VPN containers and data when rolling back. Do not use compose down -v. Docker runtime and Gluetun image versions should be reviewed before production use.