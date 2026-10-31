# MediaSeed storage compatibility (0.2.36)

Retain destination-local staging support and advertise its capability to MediaSeed, alongside configurable transfer limits. Torrent additions validate staging paths and use the existing final destination. Expired display observations are refreshed against the actual VPN/client binding; this does not create or reconcile a tunnel. Physical lifetime byte counters remain available to MediaSeed.

Validation: full suite initially 286 passed, 1 skipped and three outdated expectations; updated staging and fresh-observation expectations, then all 120 focused storage, client, observation and physical-traffic tests passed. Existing profiles, credentials, media and transfer settings are preserved.
