# MediaVPN

Verified runtime observations match the active public IP against
[Proton's published geofeed](https://ip.me/static/geofeeds/geofeed-mm.csv),
following [FjordVPN's location approach](https://github.com/qlerup/fjordvpn/blob/main/relay/observer.py).
The bounded HTTPS feed is cached for six hours, with a one-minute retry backoff.
No user IP is sent in the lookup request. The most specific matching network
supplies an ISO country code; missing, expired or unavailable data means unknown.
Generic GeoIP and profile names are not used as fallback. This supports Proton
exit addresses; other providers remain unknown unless listed in that feed.
MediaSeed receives the country and check timestamp only when its route is ready.
Location failure does not alter VPN readiness. Country does not prove physical location.

Independent MediaHub-family VPN profile manager, using FjordVPN as a functional reference, not copied application source. MediaHub Core/Agent and existing VPN installations are not required or modified.

## Current Scope

- Local administrator setup/login, session cookies, CSRF and same-origin writes.
- Multiple WireGuard profiles, bounded file upload, duplicate-key rejection, start/stop/remove.
- Full IPv4 tunnel, public IP endpoint and a restricted configuration grammar; shell hooks and arbitrary directives rejected.
- Per-profile Gluetun container with firewall enabled, no published ports, only NET_ADMIN and TUN device.
- Status requires a running container, a successful Docker healthcheck no older than 90 seconds, live Gluetun control status, public IPv4 and an active tun0 with IPv4. The Gluetun public-IP report is not an independent leak test.
- Optional Proton NAT-PMP forwarding is a persistent profile setting. Generate the WireGuard profile with NAT-PMP enabled at Proton. Its actual assigned port is read from Gluetun, never assumed from the setting. Changing this setting stops the profile; Start recreates its container if necessary.
- A bounded supervisor reconciles desired profiles and the selected MediaSeed client every five seconds. It creates a separate qBittorrent container in the selected VPN namespace, binds it to tun0, disables UPnP and synchronizes the assigned port. Missing health or port stops that client. VPN namespace changes recreate it with the same named configuration/download volumes. The original isolated client is retained stopped for rollback.
- MediaSeed integration uses a separate owner-only token file, not VPN credentials or Docker socket sharing. The machine API only permits a fixed list of torrent operations; no arbitrary Docker commands, paths, WebUI proxy or destructive file removal are accepted. Mount only the token file read-only into MediaSeed and configure MEDIASEED_VPN_URL and MEDIASEED_VPN_TOKEN_FILE.
- Standard preview saves profiles but cannot start VPNs. The privileged runtime must be deliberately enabled.

**Remaining verification/features:** independent egress/IPv6 leak tests, live stop/restart/port-change tests, TCP relay and generic app installation/update. MediaSeed attachment currently supports the existing mediaseed-preview Compose project with named volumes; other installations are rejected. Do not claim full FjordVPN parity. A saved/healthy profile alone does not prove that another app is attached.

Managed MediaHub login is optional. Set `MEDIAVPN_HUB_URL` and `MEDIAVPN_BASE_URL`
to trusted HTTPS origins, register `org.mediahub.mediavpn` at that origin in
MediaHub's `MEDIAHUB_SSO_APP_URLS`, and mount only the public hub CA with
`MEDIAVPN_HUB_CA_FILE`. The `compose.hub.yaml` overlay adds a TLS proxy on
`MEDIAVPN_TLS_PORT` (default 18803), using a separate app certificate/key in
`MEDIAVPN_TLS_PATH` and public CA file `MEDIAVPN_HUB_CA_PATH`. Keep existing runtime
overrides and protected data mounts. The restricted machine API can retain HTTP
18802 on the trusted network; browser SSO requires HTTPS. Never share the hub
private key with the app or proxy.
Set `MEDIAVPN_HUB_BROWSER_URL` when browser login uses a different public HTTPS
hub origin. It defaults to `MEDIAVPN_HUB_URL`; server-side identity checks always
use the verified `MEDIAVPN_HUB_URL`.
Saving an app domain in MediaHub settings updates the opening and SSO callback
origin together. MediaVPN reads the registration over verified TLS; the local
host and the currently registered domain are accepted. DNS, HTTPS and proxy
routing must already point to this app. Reset restores the configured local
HTTPS origin. Proxies must preserve the original Host header.

Opening the managed app automatically starts state/PKCE hub login if its app
session is missing. No separate app account or first password login is needed.
Every authenticated request checks the hub session and current app permissions.
App administrators can change runtime state; users and viewers can only read.
Managed mode disables local setup/password login while retaining existing local
accounts and profiles for rollback. Without `MEDIAVPN_HUB_URL`, standalone login
continues to work. Login does not attach MediaSeed to MediaVPN.

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

Each new profile is saved stopped. Start only through its explicit Start action. Desired started profiles are restored by the supervisor. Installing MediaVPN or starting a profile does not attach MediaSeed. Select MediaVPN and a profile explicitly inside MediaSeed, then save that choice. MediaSeed requires an observed forwarded port. Stopping the profile makes the managed torrent client unavailable and the supervisor stops it. Disconnecting inside MediaSeed disables the integration and removes the owned client container with `v=False, force=False`, preserving configuration/download volumes and leaving the VPN profile independent. MediaVPN can then be removed while MediaSeed remains installed. Removing a profile first stops/removes its owned tunnel; failures preserve the key. Profile keys cannot be downloaded through the API.

## Tests And Upgrade

### Native RSS integration

The restricted client RPC permits only MediaSeed-owned RSS item paths and NordicBytes HTTPS feed/torrent addresses. Metadata downloads run in the VPN client, reject redirects and cap torrent files at 4 MiB. Native RSS auto-download rules stay disabled; MediaSeed applies date/history approval, imports stopped, then starts an exact returned hash under a fresh gate. The native shared queue is configured for eight active downloads. Other RSS providers are rejected until their download boundaries are implemented. Existing feeds are not activated during deployment.

```sh
python -m pytest tests -q
```

Tests use synthetic keys and fake runtime objects. No live VPN access or traffic is involved. UI tests require Playwright and Chrome.

Upgrade with a new app image and the same data mount. Keep the previous image tag for rollback; no automatic destructive data migration. Preserve running VPN containers and data when rolling back. Do not use compose down -v. Docker runtime and Gluetun image versions should be reviewed before production use.
