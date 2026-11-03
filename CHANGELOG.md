# Changelog

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
