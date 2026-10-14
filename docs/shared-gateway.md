# Shared gateway

MediaVPN 0.1.1 supports one selected provider profile shared by MediaHub-managed
apps. Enabling it stops other MediaVPN profiles and prevents a second provider
profile from starting. MediaSeed must use the same profile and requires NAT-PMP
port forwarding for its download motor.

The shared gateway is opt-in through MediaVPN's Shared VPN gateway section or
the MediaHub app configuration page. Only installed supported MediaHub apps are
listed. MediaVPN delegates selection with the current package-bound SSO session;
the Hub verifies global administrator rights on every call. No management key
is exposed in the browser. It is
available only with the explicitly enabled Docker runtime. Standard unprivileged
Compose remains a preview and starts no real VPN connection.

The provider key remains in MediaVPN. Paired Agents generate independent private
WireGuard relay keys for Plex/Jellyfin; their web ports remain on their own hosts.
The gateway image installs WireGuard tools and a userspace fallback automatically.
Runtime containers require NET_ADMIN, Docker-host control and `/dev/net/tun`.
Set `MEDIAVPN_SHARED_ADDRESS` to the private LAN IPv4 address when the app's normal
origin uses a public hostname. `MEDIAVPN_SHARED_PORT` defaults to UDP 18804.

The separate `integrations/mediahub.token` authorizes Hub management. The existing
MediaSeed key cannot register other apps' peers or manage sharing. The Linux Hub
installer provisions the protected Hub-side copy; never copy runtime keys between
main and beta or include them in a source bundle.

API readiness distinguishes provider status from a relay handshake. Synthetic
tests do not establish physical egress or leak prevention. Verify behavior on the
target host with a user-provided profile before treating protection as live tested.
Stopping sharing disconnects the gateway and provider; app selections stay blocked
until explicitly disabled or the gateway is started again.
