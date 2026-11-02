# Changelog

## 1.0.0 (2026-11-01)

The library, out of the app ([liminal](https://github.com/pcxmac-dev/liminal), feature/collector at d21b4b8):
every module but the app's own, their names kept, as the package `liminal_lib`. The regular session's filter,
masks and close moved in from the chart's bars module (`sessions.py`); the market proxies became a parameter of
`collector.sync`. New: `logs.py`, the fetcher's host name and its clients' side in `host.py`, `stores.history_due`,
`sources.feed_closed`, `proc.hold_until_interrupted`, the fetcher's requests in `storage/requests.py`.
