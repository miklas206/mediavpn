# Independent app workspace

The October 13 beta UI uses the five named MediaHub environments with an independent
header selector. Selection is remembered locally in the browser. A validated
mediahub_theme query value can select a named theme when launched from the hub.

Original scenic assets and shared surface styles live in static/themes; workspace.css
contains this app's layout. No wallpaper is copied from the reference screenshots.
The UI keeps actual backend state and existing actions, authentication and network
protection. Theme changes do not configure, connect or start a VPN or torrent.

The revised environment uses abstract low-contrast light fields instead of scenic
wallpaper. Themes vary control shape, hover/press response, dialog and notification
entry. OS reduced motion disables movement.

Reference revision: Soft Orbit is now dark blue and Light is a separate choice.
The first-party hub link can pass an explicit validated Light mode.

The ambient revision adds visible abstract light fields and curved ribbons.
The shared environment stylesheet has a new cache version; no background images
are used and reduced-motion preferences disable movement.

Appearance refinement: each theme now has its own more visible abstract animated composition (glass arcs, orbit rings, terminal grid, floating planes or signal diagonals). Reduced motion remains supported; stylesheet cache version is ambient-6.

Theme identity refinement: bounded Soft Orbit control rounding prevents circular text cards. Each theme has a distinct system-font stack, icon stroke treatment and surface geometry. Hub sidebar uses different icon glyphs for Home/Overview/Settings, responding to live theme previews. Settings tabs remain visible on Appearance. Shared stylesheet version: ambient-8.
