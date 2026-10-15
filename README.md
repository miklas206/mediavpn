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
# Automatisk Proton-servervalg

Belastningsdata kræver adgang til Protons API. Den nuværende integration har
ingen Proton-kontosession; WireGuard-nøglen giver ikke API-adgang. Et HTTP
401/403-svar vises derfor som krav om API-godkendelse (`loadStatus` er
`authorization_required`), og automatisk valg bruger målt svartid. Ukendt
belastning behandles aldrig som 0 %. Andre datakilde-fejl vises særskilt som
utilgængelige data. En ny WireGuard-konfiguration løser ikke API-godkendelsen.

Upload en separat Proton WireGuard-profil med NAT-PMP aktiveret, og markér
Proton port-forwarding. Under profilens **Servervalg** kan administratoren vælge
automatisk valg i et bestemt land, automatisk valg på tværs af lande eller
beholde serveren fra konfigurationen. Dialogen viser de bedste målte lande
med servernavn, belastning og svartid. Automatisk og konfigurationens server vælges med knapper, ligesom anbefalede lande. Alle lande kan findes i en søgbar knapliste under Alle lande. Anbefalinger måles
fra MediaVPN-værten og caches i ét minut. De ændrer aldrig en aktiv tunnel.
Servervalg stopper kort den tilknyttede torrentklient og tunnelen. En aktiv eller ønsket startet profil genforbindes automatisk af supervisoren; en stoppet profil forbliver stoppet. Det gemte startønske overlever app-genstart. MediaSeed bliver bundet til den nye tunnels netværk og får dens verificerede NAT-PMP-port uden at ændre torrenternes start/stop-valg.

Ved start måles to TCP-forbindelser til port 443 på op til 24 udvalgte
P2P/WireGuard-endpoints i landet. Med belastningsdata måles de mindst belastede kandidater først; laveste belastning prioriteres, derefter gennemsnitlig svartid. Uden data vælges kandidater tilfældigt og rangeres efter svartid.
Globalt valg (`auto_country="*"`) måler en kandidat fra hvert tilgængeligt land (maksimalt 256 lande); når kataloget indeholder højst 24 lande, måles op til to kandidater pr. land. Ved kendt belastning prioriteres landets mindst belastede kandidat. Op til 24 samtidige målinger holder ventetiden nede. Det er den bedste blandt de målte endpoints, ikke en garanti for den bedste server i hele kataloget.
TCP-målingen viser ikke downloadhastighed; belastning kommer separat fra Proton. Serveren beholdes,
mens tunnelen kører; et nyt start efter stop foretager et nyt valg.
Ingen svar eller en ugyldig serverliste giver en fejl, uden automatisk fallback
til en uverificeret forbindelse. MediaSeeds eksisterende VPN-kontrol bevares.

Den offentlige [Gluetun-serverliste](https://github.com/qdm12/gluetun-servers)
hentes via HTTPS, begrænses til 2 MiB og caches i seks timer. Kun offentlige
IPv4-adresser, Proton-hostnavne og P2P-servere med gyldige WireGuard-public keys
bruges. Kataloger ældre end 120 dage afvises. Private keys sendes aldrig med
forespørgslerne. En afledt lokal `.auto.conf` med adgang 0600 bruges til tunnelen;
den uploadede `.conf` bevares. Profiler med PresharedKey kan kun bruges manuelt.
Den samme private key må ikke bruges samtidigt på en anden maskine.

Manual MediaSeed imports can explicitly choose immediate start or pause. The integration upload defaults to `start=false`; `start=true` passes consistent `stopped=false` / `paused=false` flags. RPC imports accept only matching true/false mode flags. Both modes retain the fresh readiness check under the controller lock before qBittorrent delivery. RSS imports remain paused until their existing per-item approval.

MediaSeed automatic cleanup imports may include one validated `MediaSeed-Cleanup-<24 lowercase hex>` tag. Upload and magnet RPC preserve that marker. `setShareLimits` additionally permits exactly `ratioLimit=-1`, `seedingTimeLimit=-1`, `inactiveSeedingTimeLimit=-1` so MediaSeed can enforce combined goals without native early stops. Existing finite policy validation and the readiness gate are preserved; this does not select a VPN or change its configuration.

Torrent imports and `setShareLimits` use `shareLimitAction=Stop`, including compatibility with callers that omit it. qBittorrent 5.2 requires this field for share-limit changes. Other native actions are rejected; only an explicit `torrents/delete` request with its validated `deleteFiles` flag may remove content.


Serveranbefalinger viser målt svartid og rapporteret belastning separat. Automatisk valg prioriterer laveste belastning blandt de målte, tilgængelige kandidater, derefter svartid. Belastning matches på IPv4-endpoint og WireGuard-public key fra Protons API; begge servere skal være aktive og P2P-egnede. Data caches højst ét minut. Hvis API-adgang eller data mangler, står belastning som Ukendt, og valget bruger kun svartid. En WireGuard-fil giver ikke i sig selv adgang til Protons konto-API; der gemmes ingen Proton-loginoplysninger. Opdater anbefalinger genhenter katalog og belastningsdata og måler igen uden at skifte den aktive server. IPv4 og P2P/port-forwarding er faste krav ved automatisk valg.

Latency probes run in the MediaVPN app process on its ordinary Docker bridge, not in the Gluetun tunnel or the attached client's network namespace. Recommendation refresh changes neither VPN selection nor attached-client settings. Beta verification compared the app's direct public egress with the active VPN's public IP and confirmed separate egress paths.

Country flags normalize historical region aliases and Proton's country-name variants to current ISO codes in both apps. All 145 country names in the checked public Proton catalog are covered by SVG-existence regression tests. Recommendation responses include totalCountries, and the UI states coverage and that endpoint selection remains a sample.

Managed MediaHub Overview reads `GET /api/hub/overview` with a short-lived, package-bound SSO bearer scoped to `overview`. Current hub session and app role are verified for every request. The response contains up to 100 profile names, actual connection states/countries, automatic selection mode and the supported MediaSeed consumer binding/protection state. It exposes no WireGuard keys and never changes tunnels or clients. Overview grants do not create app sessions.

## Resource limits (0.2.15)

Recreated MediaSeed torrent clients inherit the approved source container RAM, swap, CPU quota/NanoCPU and CPU affinity. MediaHub uses the `org.mediahub.resources.client-limits=1` image capability to enable persistent app resource editing. No tunnel or running client is changed merely by upgrading Core; this app remains independently updated.

### Theme-colored tab icon (0.2.16)

The image Content Security Policy permits the generated SVG data favicon, so local theme choices and inherited MediaHub appearance update the browser tab icon. Script, style and connection policies remain same-origin only. Applies on fresh installs and ordinary app updates without configuration or database changes.
