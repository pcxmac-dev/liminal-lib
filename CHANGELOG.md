# Changelog

## 1.3.0 (2026-11-03)

The class contract, plugin API 2.0 (the user: "all the plugins under liminal should basically follow
inheritance/oop paradigm"). `sources/plugin.py`: a plugin is an instance of a `FeedPlugin` or
`PublisherPlugin` subclass, what it declares class attributes, what it does methods overriding the
contract's defaults (`build`, `view`, `rate`, `has_keys`, `serves`, `markets`, `carries`,
`plan_resolutions`, `reporter`, `scan`, `settings`; a publisher's `moments`, `calendar`, `read`); the
registry loads an instance, a class (instantiated) or a module's `PLUGIN(S)`, `PUBLISHER(S)`,
`ASSISTANT(S)`. The record form (`Provider`, `Publisher`: the hooks as callback fields) is a subclass of
the same classes (`is_record`), still plugin API 1.x (`RECORD_API` 1.12; `api_refusal(..., record=True)`),
so every plugin loads as before. `Plugin.gives`, `has_scan`, `has_settings`, `settings_of`, `with_`,
`refusal`; `PublisherPlugin.has_moments`, `has_calendar`, `reads_itself` (the reader, the stores and the
views test these instead of the fields). A feed declares its publication delays and market hours
(`FeedPlugin.delays`, `hours`: `freshness.Delays`, `DELAYS.declare` when it loads; the rows of
`PLAN_DELAYS` and `FEED_HOURS` stay until every plugin carries its own). `config.load_settings` builds a
plugin's own settings for every kind (`Settings.plugin` for a publisher too). `sources/archive.py`:
`PublicFeedPlugin` (its source on the contract's public client) and `CryptoVenuePlugin` (a crypto
exchange's plan and caps, `CRYPTO_VENUE_DAYS`). `Scanned`, `Report`, `Limits`, `Capability`,
`minute_window`, `within`, `server_limits`, `server_per_minute` moved to `sources/plugin.py`, still
imported from the registry.

The AI assistant kind, `liminal_lib.assistants` (the user: three AI plugins "that have a vanilla API, are
compliant with the rest of the plugin structure ... but allow interfacing with Claude, Grock and ChatGPT
Codex via API key, an email / account name", "used to evaluate certain standardized arguments from other
plugins or apps", "extensible, and allow for higher order evaluation"). `AssistantPlugin` (`assistants/plugin.py`:
a vendor's plans, models, prices and source pages written in and dated; its keys `<NAME>_API_KEY` and
`<NAME>_ACCOUNT`, its plan variables and caps derived; its settings an `AssistantSettings`); `Assistant`
(`assistants/assistant.py`: `models()`, `evaluate(argument)`, `connect()`, the vendor hooks; `ResponsesAssistant`
for the OpenAI-shaped Responses API; `AssistantClient` on the contract's client, every error scrubbed of the key
and the account); the arguments (`assistants/arguments.py`: `Question`, `Judge`, `ContextItem`, `Answer`, a kind
registry for a plugin's own); `Evaluation` and `Usage` (`assistants/evaluation.py`), kept in the main file's
`evaluations` table (`storage/evaluations.py`); `assistants.evaluate`, `evaluate_all`, `judge`,
`default_assistant` (`LIMINAL_ASSISTANT`); `assistants/context.py` (context items from headlines, assessments,
reports, evaluations); `assistants/verify.py` (the vendor pages read again: a plugin's weekly CI test). The
registry's third kind (`assistants()`, `assistant_menu`, `ASSISTANT(S)` in a module), `config.load_settings` for
it, `sourcing.detected_plans` with its scans. `HttpClient.post` (no transport retry: an answer may have been
billed), a subclass's `_refusal` and `_final`, `limit_prefixes`; `MachineLedger.take_day(n)` and `charge_day`.
Nothing is sent from an offline copy; the caps are per machine (50 requests, 200,000 tokens a day, 1,024
output tokens, the cheapest model), raised on the Plugins view.

## 1.2.4 (2026-11-03)

1.2.3's `same_market` undone: two feeds naming no market for an asset class count as one market again (the
collector's tests run fakes that name none, and a real feed names one for every class it serves); the app's
one-feed-per-span check asks with the asset class the ticker belongs to instead.

## 1.2.3 (2026-11-03)

`coverage.same_market` says no for two feeds neither of which has a market for an asset class (it said yes, two
blanks being equal): the app's one-feed-per-span check counted Bitstamp's and Coinbase's trades of a pair no
longer watched as one market's double download.

## 1.2.2 (2026-11-03)

Found going through the fetcher's page plugin by plugin (the user: "go through the UI and look for discrepancies"):
a feed of daily bars alone (TWSE's, B3's, J-Quants', Alpha Vantage's, Cboe's) no longer lists a trades database it
can never have (`plugin_databases`: the -ticks.db file only where a feed provides 1-second bars or streams trades);
Cboe's VIX futures feed publishes a contract's day with its settlement after the session (`PLAN_DELAYS["cboe-futures"]`,
end of day; `expected` and `assess` take an end-of-day plan's futures like its stocks), where the Plugins view said
"480 min delayed", the free futures plans' rule.

## 1.2.1 (2026-11-02)

A listing outside the US whose source names another exchange keeps its exchange and still takes the record's
identity (listing date, FIGI, name, details): 1.2.0 returned before recording it, so the listing was never dated
and its daily history waited for a date that would not come.

## 1.2.0 (2026-11-02)

Stocks listed outside the US (the user: "update existing plugins, add exchanges where applicable and symbols").
One stored name per listing, `TICKER.SUFFIX` with Reuters-style suffixes of the MIC (`7203.T` Tokyo, `2330.TW`
Taiwan, `PETR4.SA` B3, `600519.SS` Shanghai, `000001.SZ` Shenzhen, `VOD.L` London, ...: `models.VENUES`); `TSE:7203`
and `7203::XTKS` normalise to it (`symbols.stock_spec`); US tickers and pins are as before. Plugin API 1.12:
`Provider.exchanges` names the MICs a feed carries ("US" for every US listing; without it a stock feed carries US
listings alone) and `can_carry` refuses the rest. A listing abroad keeps its exchange's days: every weekday is a
trading day covering the local calendar day (`MarketCalendar.local_day_session`, the seam for exchange_calendars'
holidays later), through `freshness`, `coverage` and the collector (the watch row's `exchange`), so a Tokyo trading
day on a US holiday is asked for; day bars stay stamped at midnight ET of the local date (`derive.aggregate` bins
by the local day, with no regular-session filter). Publication delays by tier name: `twse`, `tpex`, `b3`,
`alphavantage-free`, `jquants-*` (end of day; `jquants-free` twelve weeks late), `twelvedata-free` (UTC day).
`refresh_directory` asks each feed for its own exchanges' listings (stored in the suffix form), `Store.directory()`
spans every source, `Store.watch_exchanges()`, `mic_names` seeded from `VENUES`; the initializer gives a listing to
the feeds carrying it; audits, bad-data scans and the live recorder leave listings abroad alone;
`sessions.world_calendar` moved in from the app's Markets globe.

## 1.1.1 (2026-11-02)

The Resources sampler reads the app's processes under their new names: `python -m liminal_app ...` and the
console script (`liminal ...`, `liminal.exe` on Windows), the old module name still (`resources.APP_WORDS`).

## 1.1.0 (2026-11-02)

The app is liminal-app (the user: "lets rename the app to liminal-app", "not liminal", "refactor"): hosts start as
`python -m liminal_app host NAME` (`host.APP_MODULE`), messages name the `liminal` command (the app's console
script) instead of `python -m liminal`, and the library's import test refuses the app and the integrator under
their new names. Plugins import the library as `liminal_lib` from their next releases.

## 1.0.1 (2026-11-02)

The workspace (`layout.py`: `home`, `data_dir`, `plugins_dir`): `common/` the library, `app/` the chart app,
`store/` the integrator with the databases in `store/data/` (the default main file), `plugins/` a checkout per
plugin, loaded from there by the registry (`plugin_checkouts`, origin `plugins/<checkout>`; the same plugin
installed as a package besides is not loaded twice).

## 1.0.0 (2026-11-01)

The library, out of the app ([liminal](https://github.com/pcxmac-dev/liminal), feature/collector at d21b4b8):
every module but the app's own, their names kept, as the package `liminal_lib`. The regular session's filter,
masks and close moved in from the chart's bars module (`sessions.py`); the market proxies became a parameter of
`collector.sync`. New: `logs.py`, the fetcher's host name and its clients' side in `host.py`, `stores.history_due`,
`sources.feed_closed`, `proc.hold_until_interrupted`, the fetcher's requests in `storage/requests.py`.
