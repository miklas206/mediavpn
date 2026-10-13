# Independent app workspace

The October 13 beta UI uses the five named MediaHub environments with an independent
header selector. Selection is remembered locally in the browser. A validated
mediahub_theme query value can select a named theme when launched from the hub.

Original scenic assets and shared surface styles live in static/themes; workspace.css
contains this app's layout. No wallpaper is copied from the reference screenshots.
The UI keeps actual backend state and existing actions, authentication and network
protection. Theme changes do not configure, connect or start a VPN or torrent.
