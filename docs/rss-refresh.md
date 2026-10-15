# MediaSeed RSS interval

Version 0.2.19 adds the machine-authenticated GET/PUT `/api/integration/rss`
endpoint and the `rss-settings` capability. The only writable field is
`rssRefreshMinutes`, a strict integer from 1 to 1440. The range does not override
tracker polling rules.

An absent value defaults to five minutes for fresh and older managed seedbox
settings. Saving preserves all other fields and the integration token. A
verified, ready torrent client is required before saving; the desired value is
persisted before application. The client preference is read back before a
successful response. An uncertain application returns an error and the saved
desired value converges on subsequent reconciliation. Existing selected
intervals survive profile changes and client recreation.

Only the native `rss_refresh_interval` preference is changed by the setter.
Automatic native torrent downloading remains disabled; MediaSeed owns RSS
rule decisions. There are no new packages, volumes or permissions, and no
MediaHub Core update is needed. Update MediaVPN before MediaSeed 0.1.14.

Validation: 228 Python tests passed, 1 skipped, including fresh/older settings,
preserved configuration/token, read-back failures, readiness, machine auth,
strict input and reconnect/recreation with three intervals and download
limits. Production runtime has not been upgraded or restarted by this work.
