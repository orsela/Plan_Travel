# Old address redirect (orsela/Plan_Travel after the rename)

CHANGE 2026-10-12 F04-NAME-03: contents of the NEW repo `orsela/Plan_Travel`, created after `orsela/Plan_Travel` was
renamed to `orsela/Lonely_Planner`. GitHub Pages (main, root) serves these pages at the old address; each forwards to the
same sub-path under `/Lonely_Planner/`, keeping `?query` and `#hash`. `404.html` sends any other old path to the app.
