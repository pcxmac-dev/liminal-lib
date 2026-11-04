# liminal-libs

The Liminal library: the common code every Liminal program builds on (INTEGRATION.md: how the
repositories fit together; the naming: apps `liminal-<app>`, this library `liminal-libs`, plugins
`plugin-<app>-<name>`).

- [liminal-charts](https://github.com/pcxmac-dev/liminal-charts): the charts app (its front, its pages, the data API and the command line);
- [liminal-integrator](https://github.com/pcxmac-dev/liminal-integrator): the fetcher, which collects the data and serves it;
- the plugins: `plugin-integrator-<name>`, one per data provider, and `plugin-platform-<name>`, the AI
  assistants, on the contract here (`sources/plugin.py`: the classes; `sources/registry.py`: the registry).

What is here (`src/liminal_libs`): the stores (`storage/`: SQLite, DuckDB and MongoDB by data type), the plugin
contract and the registry (`sources/`), the collector (`collector.py`: the finest data each plan allows, as far
back as it allows, within its limits), the delivery scheme by cost and capability (`sourcing.py`), the calendars
and publication delays (`freshness.py`, `sessions.py`), the resident hosts over local pipes (`host.py`), the live
recorder (`live.py`), the composites, derived frames and provenance, the datasets, the publishers, and the settings
(`config.py`: keys only from your environment variables).

The workspace (`layout.py`): one folder holds `common/` (this library's checkout), `app/` (the chart app),
`store/` (the integrator, the databases in `store/data/`) and `plugins/` (a checkout per plugin, loaded from
there by the registry). Found by its four folders above this checkout, or named by `LIMINAL_HOME`.

Install: `pip install -e .` (the app and the integrator depend on it). Tests: `python -m pytest -q` (the full
suite still runs in the app's repository, with the library installed; it moves here module by module).
