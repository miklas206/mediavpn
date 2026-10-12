# Account language

MediaVPN supports Danish and English using the authenticated MediaHub account's SSO language. Open pages recheck the identity on focus and once per minute. Standalone administrators choose a language at creation; old standalone accounts retain Danish during the additive migration. Password hashes, VPN credentials and sessions are preserved.

Use English source keys with Danish translations in `static/i18n.js`. Static document labels and accessibility attributes are captured before remote content loads. Dynamic display text must use `t()` and placeholders. Preserve profile names, IP addresses, server identifiers, API values and private configuration contents. Dates, numbers and country names use the account locale.

Run `pytest tests` and `node --test tests/*.mjs` after message changes. Tests cover static labels and translation keys, standalone language persistence, old-schema credentials and reversible DOM translation without changing user data. Hub integration tests additionally exercise current language through real in-process SSO with both apps, without real VPN tunnels.
