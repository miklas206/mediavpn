# UI refresh audit, 2026-10-15

Automatic list replacement previously removed keyboard focus. MediaSeed also closed an open torrent actions menu every dashboard refresh. Interaction state is now captured from the actual focused row immediately before replacement and restored by stable torrent/source/profile identity, including when rows reorder. Unrelated focus is not moved, and removed rows are not recreated.

MediaSeed background source polling now allows at most one in-flight request per source kind. Explicit refreshes after edits still fetch new state; request generations discard superseded replies. This avoids stale pre-edit data replacing a saved result. RSS filter summaries use middle-dot separators instead of question marks.

MediaVPN ignores toggle events from detached profile disclosures, preventing old nodes from clearing expanded state.

Validation uses synthetic data and isolated Chromium, never real downloads or tunnel changes. Unit tests cover reordered/removed rows, focus outside the list, slow overlapping polls and reversed response completion. Existing login, removal, storage and localization suites remain applicable.

Fresh installs and upgrades use the same shipped static files. No schema, configuration, package, mount or runtime migration is required; existing feeds, credentials and media are retained. This is source-level verification, not a live deployment or a measurement of production tracker latency.
