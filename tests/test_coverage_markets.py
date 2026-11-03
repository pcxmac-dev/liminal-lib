"""Two feeds are one market only when both name the same market for the asset class."""
from types import SimpleNamespace

from liminal_lib.coverage import same_market

FEEDS = {"bitstamp": SimpleNamespace(venues={"crypto": "bitstamp"}), "coinbase": SimpleNamespace(venues={"crypto": "coinbase"}),
         "massive": SimpleNamespace(venues={"crypto": "consolidated", "stock": "sip"}),
         "alpaca": SimpleNamespace(venues={"stock": "sip", "crypto": "consolidated"})}


def test_two_feeds_without_a_market_for_an_asset_are_not_the_same_market():
    assert not same_market(FEEDS, "bitstamp", "coinbase", "crypto")
    assert not same_market(FEEDS, "bitstamp", "coinbase", "stock")  # neither has stocks: not one market either
    assert same_market(FEEDS, "massive", "alpaca", "stock") and same_market(FEEDS, "massive", "alpaca", "crypto")
    assert not same_market(FEEDS, "massive", "nowhere", "stock")
