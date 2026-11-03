"""Two feeds are one market when they name the same market for the asset class; naming none, both,
counts as one too (the collector's tests run fakes that name none): the app asks with the class the
ticker belongs to, so a crypto pair's spans on two exchanges are two markets."""
from types import SimpleNamespace

from liminal_lib.coverage import same_market

FEEDS = {"bitstamp": SimpleNamespace(venues={"crypto": "bitstamp"}), "coinbase": SimpleNamespace(venues={"crypto": "coinbase"}),
         "massive": SimpleNamespace(venues={"crypto": "consolidated", "stock": "sip"}),
         "alpaca": SimpleNamespace(venues={"stock": "sip", "crypto": "consolidated"}),
         "fake": SimpleNamespace(venues={}), "other": SimpleNamespace(venues={})}


def test_feeds_are_one_market_when_they_name_the_same_one():
    assert not same_market(FEEDS, "bitstamp", "coinbase", "crypto")
    assert same_market(FEEDS, "massive", "alpaca", "stock") and same_market(FEEDS, "massive", "alpaca", "crypto")
    assert not same_market(FEEDS, "massive", "bitstamp", "crypto")
    assert same_market(FEEDS, "fake", "other", "stock")  # neither names one: one market, as the fakes expect
