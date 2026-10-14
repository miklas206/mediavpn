# Guided Proton gateway (0.2.0)

Open MediaVPN through MediaHub and use **Shared VPN gateway**. The guide covers
requirements, the uploaded Proton profile, scoped Cloudflare access, and selected
installed app domains. MediaHub resolves private app addresses and manages owned
DNS/Origin Rules; the browser cannot submit arbitrary private upstreams.

Required: managed Linux Docker on amd64/arm64, TUN, writable private data, a paid
Proton P2P WireGuard configuration with NAT-PMP enabled, and a Cloudflare zone
in Full (strict) mode. The token needs Zone Read, DNS Edit, Origin Rules Edit and
Zone Settings Read for the chosen zones. Host checks run before activation.

Traefik 3.3.7 runs inside the selected provider namespace. The existing Hub
Traefik remains outside it. The gateway's public listener accepts only configured
domains from Cloudflare socket addresses; forwarded-header spoofing cannot bypass
that check. Local HTTPS verifies its configured CA and server name. Cloudflare
credentials are private file mounts; ACME state is mode0600. No Caddy/Go build
dependency is included.

The first provider forwarded port serves HTTPS. A separate capability-limited
NAT-PMP helper renews a distinct matching TCP/UDP lease for MediaSeed. It receives
neither Cloudflare credentials nor the provider key. qBittorrent's private listen
port and public announce port are checked independently. Stale leases, wrong
provider generation and port collision block downloads instead of falling back
to ordinary internet.

The machine API is GET/PUT `/api/integration/web-gateway`, authorized by the
separate `X-MediaHub-Key`. GET exposes prerequisite, provider, certificate and
runtime evidence separately. Public end-to-end HTTPS is verified by MediaHub
using the exact gateway response marker. A saved configuration is not a claim
that ACME, DNS or the user's provider is working.

MediaHub prepares native helper images during fresh installation and updates.
Standalone runtime may build a missing reviewed helper on activation, reporting
its progress; it never starts a provider in preview mode. Configurations and
certificates are stored below the app's existing private data directory.

Existing Cloudflare tunnels are not removed by app updates. Conflicting hostnames
need a free name or explicit migration. Disable public domains and wait for
owned-record cleanup before replacing the Cloudflare credential.

Unit tests, actual offline Traefik routing and a synthetic UDP NAT-PMP peer are
verified. Actual Proton multiple-lease acceptance, ACME issuance, public SSO and
physical loss-of-VPN behavior still need verification on the selected host.

See the [MediaHub gateway guide](https://github.com/miklas206/mediahub-platform/blob/main/docs/vpn-gateway-guide.md).
