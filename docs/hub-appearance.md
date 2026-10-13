# MediaHub appearance and app overrides

Managed HTTPS/PKCE SSO sessions receive current per-user appearance and the hub base color mode through the existing identity check. Authenticated `/api/auth/me` exposes only public appearance fields, plus `managed` and the app-specific `themeOverride`. Existing permissions, token validation and session revocation remain authoritative.

The default is Follow MediaHub: theme, custom accent/secondary/background colors, light/dark/system mode, compact density and reduced motion follow the user account. Re-read identity on browser focus, tab visibility and once per visible minute. Preference refreshes do not overlap. Navigation structure remains app-specific.

Choose another theme in the app header to override its style/colors for this user in this app. This preserves inherited compact/reduced-motion preferences and color mode (the legacy Light choice explicitly selects light mode). `PUT /api/auth/preferences` accepts only `themeOverride` (known theme or null), requires the current managed session and CSRF, and is permitted for viewers because it only edits their own appearance. The user ID always comes from authenticated SSO, never the request body. A small additive `hub_theme_preferences` table persists the choice by hub user ID; it contains no tokens or credentials. Null deletes the override and resumes following immediately. Changes do not write MediaHub preferences or another app's data.

Standalone installations retain their existing local theme picker. Managed appearance is never written into the standalone localStorage key. Old hubs without appearance fields fall back to the default theme.

The color engine is generated from MediaHub's pure TypeScript appearance module and preset catalog using `node scripts/sync-app-appearance.mjs` in fjordcopy/frontend. Keep generated bundles identical across both apps and rerun the parity test when changing tokens/presets. Assets are versioned to refresh browser caches. No external fonts or images are fetched.

Rollback keeps existing app databases, credentials, external mounts and media. The new preference table is additive and harmless to older app versions.
