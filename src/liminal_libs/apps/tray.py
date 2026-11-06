"""The tray: an app's icons on the right of its page (the user, 2026-11-04: "every app ... has a menu
on the right-hand side with icons"; "each app is only going to have one master icon, that's kind of
controls the app itself ... all the other icons that are going to be on the right-hand side will be
app plugins"; "Each divider represents a separate tray. From a separate object like the login or
the platform or the integrator or the charts"). An app's tray is its master icon (`kind` "app")
followed by its view plugins' icons (`kind` "plugin"); a super app draws the trays of the apps it is
bound to one under another, a divider between (0.3.0). What travels is `as_dict()`: `GET /api/tray`."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class TrayItem:
    """One icon: the view it opens (`id`), its icon's name (icons.js), its tooltip, whether it is
    the app's master icon or one of its plugins', and the app it belongs to."""

    id: str
    icon: str
    title: str
    kind: str = "plugin"  # "app": the master icon; "plugin": a view plugin of the app
    app: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Tray:
    """An app's tray: the app, its master icon and title, its items (the master first)."""

    app: str
    icon: str
    title: str
    items: tuple[TrayItem, ...] = ()

    def as_dict(self) -> dict:
        return {"app": self.app, "icon": self.icon, "title": self.title, "items": [i.as_dict() for i in self.items]}
