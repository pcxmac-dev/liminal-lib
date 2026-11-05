# Changelog

## 1.6.0 (2026-11-04)

The data specs, plugin API 2.1 (`sources/data.py`; the user, 2026-11-03: "every plugin needs to furnish
the correct mechanisms for assessing data, data integrity, provide a 'spec' for the data, different
source plugins like massive and Tingo can inherit 'stock_symbol_data'. obviously the api for each
source provider is different so the calls are inherent (external calls to source) of the plugin
itself"). A class per kind of data a plugin delivers, three methods each: `spec()` (the fields and
dtypes of a bar or tick row, the resolutions, the sessions and calendar, the adjustments, the symbol
form; a document's parts; the products and the store each goes to), `assess(frame, since, until)`
(what is held and what is missing, as the coverage ledger's spans; completeness against the calendar's
slots; latency; with a plan, freshness's state: what the coverage planner and the integrator's
assessor read) and `integrity(frame)` (the bad-data rules behind one method, `baddata.malformed` and
`baddata.spikes` called, not copied; duplicates, rows outside the sessions, gaps inside them, a
required field missing; `compare` for two feeds' copies of a span, `audit.compare`; `scan` for the
rules that need the store, `baddata.scan_series`). The fetching stays in each plugin's DataSource.

- `TimeSeriesData`, the feeds' bars and ticks: `StockSymbolData` (the user's stock_symbol_data: the
  consolidated tape's hours, split-adjusted prices, TICKER or TICKER.SUFFIX), `IndexData` (regular
  hours, no volume), `FxData` (Sunday 17:00 to Friday 17:00 ET, day bars UTC days), `CryptoSymbolData`
  (around the clock, UTC days, a market per exchange so two copies' volumes are not judged, trades
  without sale conditions) and `FutureData` (CME sessions, settlement dates, no adjustment at a roll);
  `slots` (the bar starts the calendar expects), `rows` and `wire` (a frame in either shape, the feeds'
  or the store's). A feed plugin declares its own where the vendor's data differs
  (`FeedPlugin.data = {"stock": MassiveStockData}`, a subclass of the asset class's) and the contract
  gives the asset class's otherwise (`FeedPlugin.data_for(asset)`), so every plugin conforms unchanged.
- `DocumentData`, the publishers' documents, with the parts `Paragraph`, `Page` and `Statement` and
  `Document` (the original as the stores hold it, with its pages and the statements parsed from it):
  `ReleaseData` (an agency's items, GOV_ITEMS), `HeadlineData` (a vendor's headlines, NEWS) and
  `FilingData` (a filing's pages and statements: the `FILINGS` kind, to be registered in
  storage/documents.KINDS when filings are fetched); a document's completeness is its required
  fields, every page in its run and every statement parsed through; a publisher declares its class
  (`PublisherPlugin.data`) or takes its kind's (`PublisherPlugin.document_data()`: a vendor's
  headlines, every other's releases).
- Storage, in the classes (the user: "mongo is used to store the original source document, while a
  different database is used to evaluate or derive information from the original document"): each
  class's `products` (`Product`: the Store picker's type, the engines, where) name the original (a
  document to MongoDB, or SQLite until it moves; a feed's bars to its file, its closed months to
  DuckDB, its ticks) and what is evaluated or derived from it (parsed statements, the AI evaluations,
  the bars built here, the pruned list), kept apart in SQLite or DuckDB, keyed to the original
  (`keyed_to`) and never written back; `check()` holds a class to it. Nothing moves today.
- The contract: `registry.PLUGIN_API` 2.1; a 2.0 plugin loads unchanged (`api_refusal`); a plugin
  declaring `data` says `api = "2.1"`; `Plugin.api`'s default stays 2.0. The conformance check reads
  the declarations: a feed's `data` maps its asset classes to subclasses of theirs, a publisher's is
  a DocumentData subclass or None, each class a plugin answers with gives its spec and passes its own
  `check()`, a declaration needs 2.1 or later.
- Tests: tests/test_data_specs.py (each class's spec and slots, an assessment with known gaps and
  latency, an integrity report on malformed, duplicate, out-of-session and spiky rows, a document's
  parts and completeness, the contract's defaults and a plugin's own, the conformance rule, a 2.0
  plugin loading under 2.1). The charts app pins the contract's version in two tests of its own
  (tests/test_plugins.py: `PLUGIN_API == "2.0"`, and a plugin for 2.1 expected to be left out): they
  move with the app's pin of the library.

## 1.5.1 (2026-11-04)

`collector.sync` is a plain wrapper of `Collector(...).run(...)` again: the charts app's test reads the
run's first stage (`Collector._before_symbols`) since the app moved to the classes, so the copied
signature and `sync.__wrapped__` go.

## 1.5.0 (2026-11-03)

The library's core classes (Phase 5 of the object-oriented rewrite; the user: the plugins and apps
"follow inheritance/oop paradigm"). The rule: a class owns a run's or a view's state and the
decisions; the hot loops and the pure functions stay functions; every module keeps a function under
each name a caller or a test used, so nothing that imports the library changes (the charts app and
the integrator move to the classes in their own commits); a class calls the module's functions
through the module (a test's stand-in is the one used) and reads the module's constants when a
stage runs, never when it is made.

- The runs (5a). `collector.Collector(source, store, tier, *, now, log, refresh_hours, feeds, audits,
  futures_per_run, proxies)`: a feed's scheduled run, stage by stage (`_before_symbols`, `_plan_run`,
  `_step(item, urgent)`, `_after_symbols`; `run(only, published_only, on_item, directory_allowance) ->
  SyncResult`); `sync(...)` is `Collector(...).run(...)`, its source the class's (inspect.getsource; the
  signature its own). `RunQueue` (the run's rows on the fetch queue; `_Queue` kept); `SYMBOLS_PER_MONTH`
  a mapping that reads the plugins' monthly caps when asked, not when the module loads, so
  `monthly_cap` is lazy. `audit.Auditor(source, store, me, feeds, now, log, rng)` (`run`, `span`,
  `random_span`, `window`; its watch rows read once). `initializer.Initializer(store, feeds,
  futures_feeds, source, now).run(exchanges, by, dry_run)`. Stay functions: sync_bars, _fill, _plan,
  verify_fetched, sync_future, check_listing, daily_first, market_days, refresh_directory,
  fill_directory_icons, scan, history_floor; audit.compare, box, spot, allowance, audit_span.
- The planning (5b). `coverage.Planner(store, feeds, now).plan(me, ticker, asset, timespan, floor,
  listed_from, exchange)`: the audit scores and the standing scheme read once when first needed, a
  ticker's watch rows and a feed's quality the first time they are asked for (`forget(ticker)` reads
  the rows again), the coverage ledger for every plan; the collector makes one planner per symbol
  step, per daily-first sweep and per scan. `sourcing.Scheme.build`, `Scheme.standing` (the cache the
  class's, by store), `Scheme.reports`, `Scheme.offered` (the field `offers` is the offers a scheme
  was built from) and `scheme.text(reports)`; `sourcing.Scans` (`keep`, `last`, `read`, `at`,
  `plans`, `apply`): the providers' capability scans (kv SCAN_KEY) in one place. `derive.Builder(store,
  log)` (`chain`, `symbol`, `run`) and `derive.Lease(store, source, ticker)`, a context manager over
  the build leases; `baddata.Scanner(store, log)` (`symbol`, `repair_from_ticks`, `entries`). Stay
  functions: frontier_ms, subtract, intersect, eras_ahead, has_open_time, latency, rank, same_market;
  derive.aggregate (vectorised), the marks and leases; malformed, spikes, inconsistent, scan_series.
- The settings and the services (5c). `config.Settings.load(env, env_file)` and
  `settings.with_detected(plans)`; `config.UserEnvironment(registry, names)` (`saved`, `save`): the
  user's saved environment variables, the winreg code (a stand-in registry works on any OS).
  `datasets.DatasetFetcher(store, today, session, log)` (`fetch`, `files`, `git`) and `FileQueue`
  (`_FileQueue` kept). `publications.Reader.for_view`, `Reader.view`, `Reader.run_due`,
  `Reader.recheck_now`. `killswitch.KillSwitch(store)` (`killed`, `strike`, `release`, `kills`).
  `runner.Runner(store_factory, settings, log)` (`run`, `plugin`). `live.Recorder.for_settings(settings,
  locks, stop, log)`: the charts app's `make_recorder`. `host.Attachment.to(name, db_path, start, wait,
  log)` and `host.Host.start(name, db_path)` (a host's wait for its first client is `start_wait` now);
  `attach`, `start_host` and `attach_or_start` kept (the integrator imports them by name).
- Stays functional, deliberately: `sessions.py` (the calendar masks), `freshness.expected` and the
  publication rules, `storage/*` (the queries), `stores.py`, `memory.py`, `resources.py`, `proc.py`,
  `ratelimit.py` (classes already), `gov.view`, `gov.parameters`, `gov.upcoming`.
- Tests: tests/test_run_classes.py, tests/test_planning_classes.py, tests/test_settings_classes.py (each
  class's state across calls, the wrappers, a stand-in set on the module used by the class, a constant
  read when the run runs). With another checkout of the library installed editable, the suite needs
  this checkout's `src` first on PYTHONPATH.

## 1.4.2 (2026-11-03)

The recorder's command pattern (`live.RECORDER_COMMAND`: a stray recorder ended by the one holding the
machine lock) reads the new names, `-m liminal_charts live` and `liminal-charts live`, and the old ones
still (a process started before the rename).

## 1.4.1 (2026-11-03)

The class contract's conformance check, `sources/conformance.py` (the user: "evaluate each functional
piece of software or plugin and verify correctness and valid functionality through unit tests created to
address new class scheme"): `problems(plugin)` lists every way a plugin falls short of the contract, read
from the plugin alone (its name, title, contract version, system and kind, keys and variables, hooks,
`with_()`, and what its kind declares: a feed's assets, resolutions, delays and hours; a publisher's feeds
and datasets; an assistant's plans, models, prices, sources and caps); `assert_conforms`,
`assert_module_conforms` and `plugins_of` for the tests: every plugin package's tests/test_contract.py,
and the charts app's test over every plugin found.

## 1.4.0 (2026-11-03)

Renamed `liminal-libs` (the package `liminal_libs`): the naming by system (the user, 2026-11-03: apps are
`liminal-<app>`, the one library `liminal-libs`, plugins `plugin-<app>-<name>`). The charts app is
`liminal-charts` (`liminal_charts`, the command `liminal-charts`; `host.APP_MODULE`), the stock-data
plugins `plugin-integrator-<name>`, the AI plugins `plugin-platform-<name>`; `resources.role_of` reads
the new names and the old (a task registered before). INTEGRATION.md: how the repositories fit
together. Every import of `liminal_lib` becomes `liminal_libs`; nothing else changes.

## 1.3.1 (2026-11-03)

Which plugins a process loads is a selection (`registry.Selection`, LIMINAL_PLUGINS_LOADED; `select`,
`selection`, `plugins_loaded`): every plugin found by default, the ones named (each with its parts: boats
keeps Alpaca, sec keeps Federal), or none at all (the user: "the default case is to load all plugins,
however the integrator can be loaded with select plugins or none"; the integrator's `--plugins`). The
plugins found but not named are left out quietly (`Selection.left_out`, not `errors()`), a name no plugin
answers to is noted (`unknown`), and a process started by this one inherits the selection. LIMINAL_PLUGINS_LOADED
and LIMINAL_INTEGRATOR_NAME (an integrator of its own, liminal-integrator 1.4.0) are the app's variables
(`keys.APP`), no plugin's.

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

The AI assistant kind, `liminal_libs.assistants` (the user: three AI plugins "that have a vanilla API, are
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

The Resources sampler reads the app's processes under their new names: `python -m liminal_charts ...` and the
console script (`liminal ...`, `liminal.exe` on Windows), the old module name still (`resources.APP_WORDS`).

## 1.1.0 (2026-11-02)

The app is liminal-charts (the user: "lets rename the app to liminal-charts", "not liminal", "refactor"): hosts start as
`python -m liminal_charts host NAME` (`host.APP_MODULE`), messages name the `liminal` command (the app's console
script) instead of `python -m liminal`, and the library's import test refuses the app and the integrator under
their new names. Plugins import the library as `liminal_libs` from their next releases.

## 1.0.1 (2026-11-02)

The workspace (`layout.py`: `home`, `data_dir`, `plugins_dir`): `common/` the library, `app/` the chart app,
`store/` the integrator with the databases in `store/data/` (the default main file), `plugins/` a checkout per
plugin, loaded from there by the registry (`plugin_checkouts`, origin `plugins/<checkout>`; the same plugin
installed as a package besides is not loaded twice).

## 1.0.0 (2026-11-01)

The library, out of the app ([liminal](https://github.com/pcxmac-dev/liminal), feature/collector at d21b4b8):
every module but the app's own, their names kept, as the package `liminal_libs`. The regular session's filter,
masks and close moved in from the chart's bars module (`sessions.py`); the market proxies became a parameter of
`collector.sync`. New: `logs.py`, the fetcher's host name and its clients' side in `host.py`, `stores.history_due`,
`sources.feed_closed`, `proc.hold_until_interrupted`, the fetcher's requests in `storage/requests.py`.
