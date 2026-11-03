"""What a plugin's data is in (registry.plugin_databases): a feed has a trades file only where it can
have trades (1-second bars among what it provides, or a stream); a feed of daily bars alone has none
(seen on the Plugins view, 2026-11-03: every feed listed "<feed>-ticks.db: trades")."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from liminal_lib.sources import registry
from liminal_lib.sources.registry import Provider, plugin_databases
from liminal_lib.sources.stream import Live


@pytest.fixture
def installed(monkeypatch):
    def install(*plugins):
        eps = [SimpleNamespace(name=p.name, value=f"fake_{p.name}:PLUGIN", load=(lambda p=p: p)) for p in plugins]
        monkeypatch.setattr(registry, "_entry_points", lambda: eps)
        return registry.reload()

    yield install
    registry._providers = None


def _p(name, **kw):
    return Provider(name=name, title=name.title(), build=lambda s, b: None, assets=frozenset({"stock"}), **kw)


def test_a_feed_of_daily_bars_alone_has_no_trades_database(installed):
    installed(_p("daily", provides={"stock": ("day",)}),
              _p("ticky", provides={"stock": ("second", "minute", "day")}),
              _p("streamy", provides={"stock": ("minute",)}, live=(Live("streamyiex", "stock", stream=object),)),
              _p("polled", provides={"stock": ("day",)}, live=(Live("polledboats", "stock", poller=lambda s: None),)),
              _p("plain"))  # nothing declared: what it provides is not known, its trades file is listed
    holds = {n: [d.holds for d in plugin_databases(n)] for n in ("daily", "ticky", "streamy", "polled", "plain")}
    assert holds["daily"][:2] == ["daily: bars", "daily: closed months' bars, a DuckDB file a month (when its bars store is DuckDB)"]
    assert not any("trades" in h for h in holds["daily"])
    assert "ticky: trades" in holds["ticky"] and "plain: trades" in holds["plain"]
    assert "streamy: trades" in holds["streamy"] and "streamyiex: trades" in holds["streamy"]  # its stream's trades
    assert "polledboats: bars" in holds["polled"] and not any("trades" in h for h in holds["polled"])  # a poller: bars
    assert holds["daily"][-1].startswith("its rows by feed")  # the main file, last
