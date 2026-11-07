"""The apps' frame (liminal-libs 1.7.0; the user, 2026-11-04: "define all this stuff in liminal
libs ... the library basically facilitates the interprocess communication, the way the apps can
bind to each other, or interoperate, or seamlessly connect ... As well as the framework for the
graphics"): `App` (an app's page, tray and routes served at its own port: app.py), `Tray` and
`TrayItem` (its icons: tray.py), `LoginClient` (what any program asks the login app: login.py).
What is particular to one app (its users, its views) stays in that app's package."""
from .app import App, Refused, make_handler  # noqa: F401
from .authorization import EnvironmentLogin, Login, Unauthorized  # noqa: F401
from .binding import FORWARDED, PASSED_BACK, START_SECONDS, Binding, forward, start_app, tray_of, wait_for  # noqa: F401
from .login import DEFAULT_URL, SESSION_HEADER, URL_VAR, LoginClient, LoginRefused, LoginUnreachable  # noqa: F401
from .tray import Tray, TrayItem  # noqa: F401
