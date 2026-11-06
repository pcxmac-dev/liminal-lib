"""The graphics the apps share (the user, 2026-11-04: the library holds "the framework for the
graphics. The common themes and drawing methods"): `static/icons.js` (the rail's icons, a symbol's
logo, the data-state icons; `LiminalIcons.icon(name, size)`) and `static/liminal.css` (the themes'
variables, the base controls, the rail, a small app's page). An app serves them beside its own files
(apps.App through web.static_file: its folder first, then STATIC here)."""
from pathlib import Path

STATIC = Path(__file__).parent / "static"
