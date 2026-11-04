"""The class contract's conformance check (sources/conformance.py): what every plugin must satisfy,
read from the plugin alone (nothing sent); a plugin that falls short is named with each problem. The
user, 2026-11-03: "evaluate each functional piece of software or plugin and verify correctness and
valid functionality through unit tests created to address new class scheme"."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from liminal_libs.sources.conformance import assert_conforms, assert_module_conforms, plugins_of, problems
from liminal_libs.sources.keys import KeyVar
from liminal_libs.sources.plugin import FeedPlugin

from test_plugin_classes import AcmeFeed, AcmeNews, acme_record


def test_a_conforming_feed_publisher_and_record_pass_and_a_module_gives_its_plugins():
    assert problems(AcmeFeed()) == [] and problems(AcmeNews()) == [] and problems(acme_record()) == []
    assert_conforms(AcmeFeed())
    module = SimpleNamespace(__name__="fake_acme", PLUGINS=(AcmeFeed(), acme_record("zenith")), PUBLISHER=AcmeNews())
    assert [p.name for p in plugins_of(module)] == ["acme", "zenith", "acme-news"]
    found = assert_module_conforms(module)
    assert [p.name for p in found] == ["acme", "zenith", "acme-news"]
    assert {p.SYSTEM for p in found} == {"integrator"} and {p.KIND for p in found} == {"feed", "publisher"}


def test_what_falls_short_is_named():
    class Bad(FeedPlugin):
        name = "Bad Name"
        title = ""
        api = "1.0"
        assets = frozenset({"bonds"})
        keyless = frozenset({"stock"})
        keys = (KeyVar("LIMINAL_DB"), KeyVar("lower"))
        plan_vars = ("BAD_PLAN",)
        plan_choices = {"OTHER": ("a",)}
        provides = {"stock": ("hour",)}
        kind = "guess"
        exchanges = frozenset({"nyse"})
        delays = {"free": {"stock": "eod"}}
        hours = {"bonds": "x"}

    text = "\n".join(problems(Bad()))
    for expected in ("name 'Bad Name'", "no title", "api '1.0'", "assets ['bonds']", "keyless", "'LIMINAL_DB' is the app's own",
                     "'lower' is not a variable name", "plan_choices for 'OTHER'", "provides 'stock', not one of its assets",
                     "provides['stock']", "kind 'guess'", "exchanges", "delays['free']['stock']", "hours",
                     "a feed must give build()"):
        assert expected in text, expected
    with pytest.raises(AssertionError, match="no title"):
        assert_conforms(Bad())
    assert problems(SimpleNamespace(name="x")) == ["not a plugin: SimpleNamespace is no Plugin subclass's instance"]
    assert any("contract's own FeedPlugin" in w for w in problems(FeedPlugin(name="raw")))
    with pytest.raises(Exception, match="defines no"):
        assert_module_conforms(SimpleNamespace(__name__="empty"))
    with pytest.raises(AssertionError, match="no title"):
        assert_module_conforms(SimpleNamespace(__name__="bad", PLUGIN=Bad()))


def test_a_publisher_and_an_assistant_that_fall_short_are_named():
    from liminal_libs.assistants.assistant import Caps, Model, Plan, Source
    from liminal_libs.assistants.plugin import AssistantPlugin
    from liminal_libs.sources.publisher import Feed, PublisherPlugin

    class LoosePress(PublisherPlugin):
        name = "loose"
        title = "Loose"
        feeds = (Feed("a", "A", "one", "https://a.example/rss"), Feed("a", "A", "twice", "https://a.example/rss2"))
        policy = None
        datasets = ("x", "x")

    text = "\n".join(problems(LoosePress()))
    assert "a feed key twice" in text and "policy must be" in text and "a dataset twice" in text

    class LooseAssistant(AssistantPlugin):
        name = "loose-ai"
        title = "Loose AI"
        vendor = ""
        plans = (Plan("free", "", api_access=False), Plan("free", "Free again"))
        models = (Model("m1", "M1", -1.0, 2.0, plans=("pro",)), Model("m1", "M1 again", 1.0, 2.0))
        default_model = "m2"
        verified = "yesterday"
        sources = (Source("http://plain.example", "", ""),)
        caps = Caps(0, 1, 1)

    text = "\n".join(problems(LooseAssistant()))
    for expected in ("no vendor", "assistant must be an Assistant subclass", "a plan twice", "plan 'free' has no title",
                     "a model twice", "negative price", "unknown plans ['pro']", "default_model 'm2'", "https url",
                     "verified 'yesterday'", "positive limits"):
        assert expected in text, expected
