# Connections and queue settings

MediaSeed 0.1.38 adds Settings → Connections and queue. When MediaVPN manages the client, update MediaVPN to 0.2.35 first. Older gateways show a concrete upgrade message and cannot receive unverified settings writes.

Administrators can choose total/per-torrent connections, total/per-torrent upload slots, simultaneous downloads and simultaneous seeding torrents. Each has an Unlimited option. Connections serve both directions; upload slots are recipients, not torrent counts or speed limits. Forced starts bypass queue limits. Existing slow-torrent counting behavior is preserved.

Fresh installs and upgrades read the client's actual values. Opening the form does not change them. No new dependencies or configuration migration are required. Saving enables the queue and removes the combined torrent-count cap so the two displayed queue limits can operate independently. Only the six supported preference fields and these two queue fields can be written; interface, port, VPN, paths, authentication and rate limits are untouched. No torrent start/stop RPC is issued.

MediaVPN stores the five new values in seedbox.json transferLimits, with maxActiveDownloads retaining its existing top-level representation. Both survive restarts/reconnects and coexist with the old download-settings API. Missing transferLimits preserves the existing connection/upload-slot preferences. Writes read back the live client settings before persisting/reporting success; failed verification attempts restore the previous preference values. Settings are restricted to administrators/CSRF in MediaSeed and the existing machine credential in MediaVPN.

Validation: synthetic native and gateway API tests, authentication/CSRF/role checks, strict integer and field validation, failed-apply rollback, fresh/older configuration preservation, and managed client recreation/port-change tests. Browser tests cover Danish desktop/mobile layouts, saving and reopening, Unlimited, read-only users and unavailable older gateways. No real torrents or VPN sessions are started during testing.

Reference: qBittorrent WebUI API 5.0 preferences and qlerup/fjordseed main bf3d8cf68ecbd4dd5ebd39a9bc27023c261b6277 (native queue preferences and preservation of paused torrents).
