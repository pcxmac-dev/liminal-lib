# Changelog

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
