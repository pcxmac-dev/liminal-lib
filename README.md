# liminal-lib

The Liminal library: the common code every Liminal program builds on.

- [liminal](https://github.com/pcxmac-dev/liminal): the chart app (its front, its pages, the data API and the command line);
- [liminal-integrator](https://github.com/pcxmac-dev/liminal-integrator): the fetcher, which collects the data and serves it;
- the plugins, `liminal-plugin-<name>`: one per data provider, on the contract here (`sources/plugin.py`: the
  classes; `sources/registry.py`: the registry).

What is here (`src/liminal_lib`): the stores (`storage/`: SQLite, DuckDB and MongoDB by data type), the plugin
contract and the registry (`sources/`), the collector (`collector.py`: the finest data each plan allows, as far
back as it allows, within its limits), the delivery scheme by cost and capability (`sourcing.py`), the calendars
and publication delays (`freshness.py`, `sessions.py`), the resident hosts over local pipes (`host.py`), the live
recorder (`live.py`), the composites, derived frames and provenance, the datasets, the publishers, and the settings
(`config.py`: keys only from your environment variables).

The modules keep their names from the app they came out of: the app imports them as `liminal.<module>` through
its own package until every import points here (`import liminal_lib.<module>`).

The workspace (`layout.py`): one folder holds `common/` (this library's checkout), `app/` (the chart app),
`store/` (the integrator, the databases in `store/data/`) and `plugins/` (a checkout per plugin, loaded from
there by the registry). Found by its four folders above this checkout, or named by `LIMINAL_HOME`.

Install: `pip install -e .` (the app and the integrator depend on it). Tests: `python -m pytest -q` (the full
suite still runs in the app's repository, with the library installed; it moves here module by module).
