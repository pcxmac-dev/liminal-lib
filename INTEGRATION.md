# How the Liminal repositories fit together

A brief for anyone touching more than one repository (2026-11-03). The long form of each part is
`docs/ARCHITECTURE.md` in liminal-charts; each repository's README and CHANGELOG say what it is and
what changed.

## The pieces

| Repository (pcxmac-dev) | Python package | Command | What it is |
|---|---|---|---|
| `liminal-libs` | `liminal_libs` | – | The library every program and plugin builds on: the stores, the plugin contract and registry, the collector, the delivery scheme, the calendars, the resident hosts, the live recorder, the settings. |
| `liminal-charts` | `liminal_charts` | `liminal-charts` | The charts app: the chart page, its data and monitoring views, the command line; a front that funnels every request of its pages to the fetcher. |
| `liminal-integrator` | `liminal_integrator` | `liminal-integrator` | The fetcher: a stack of requests, an assessment of the work over the plugins, the standing pass, the data API page, a service or daemon from your logon. |
| `plugin-integrator-<name>` (fifteen) | `plugin_integrator_<name>` | – | The stock-data plugins of the integrator: feeds (Massive, Alpaca, Tiingo, the crypto venues, Cboe, TWSE, B3, Alpha Vantage, J-Quants, Twelve Data) and publishers (Federal, Ken French, Datahub). |
| `plugin-platform-<name>` (three) | `plugin_platform_<name>` | – | The AI assistants of the platform: Claude, Grok, Codex, each on your own API key, evaluating standardized arguments. |

Coming with 0.3.0: `liminal-platform` (the machine's information, the AI plugins' host, the IPC nexus
and the base class of the charts app), `liminal-login` (authorization), `liminal-plugins` (the
plugins-manager serving every plugin of every app), and the charts app's own plugins
`plugin-charts-<name>` (analysis, trading, watchlist, preferences).

## The naming convention

- Apps, the trays in the GUI: `liminal-<app>`, the short name being the app's (charts, integrator,
  platform, login, plugins). The command line is the full name (`liminal-charts gui`,
  `liminal-integrator serve`).
- The one common library: `liminal-libs`.
- Plugins: `plugin-<app>-<name>`, named by the app they plug into: `plugin-integrator-massive`,
  `plugin-platform-claude`, `plugin-charts-analysis`.
- Python packages are the same names with underscores (`liminal_charts`, `plugin_integrator_massive`);
  a plugin's registry name is its own short name (`massive`, `claude`), its entry point under the
  group `liminal.providers`.

## The workspace on disk

One folder holds the four (`liminal_libs.layout`: found by the folders above the library's
checkout, or named by `LIMINAL_HOME`):

```
liminal/
  common/    liminal-libs (this repository)
  app/       liminal-charts (its .venv inside: every program is installed editable there)
  store/     liminal-integrator, with the databases in store/data/ (never in git; LIMINAL_DB overrides)
  plugins/   a checkout per plugin, loaded from there by the registry (LIMINAL_PLUGINS names another folder)
```

On the user's PC the folder names stay `common`, `app`, `store`, `plugins` whatever the repositories are called.

## Who depends on whom

- A plugin depends on `liminal-libs` alone, and no plugin imports another. Its CI installs the
  library from the branch it is tested against (`LIMINAL_LIB_REF`).
- `liminal-integrator` depends on `liminal-libs` and, for the page and data API it serves, on
  `liminal-charts`; `lib.py` is the one place in it that names either. (At 0.3.0 the page front
  moves out of the charts app into the platform, so the integrator stops importing the charts app.)
- `liminal-charts` depends on `liminal-libs`; its `integrator` extra brings the fetcher, its
  `plugins` extra every plugin, each pinned by git reference in `pyproject.toml`.
- Nothing imports a plugin by name (the charts app's tests enforce it). The registry
  (`liminal_libs.sources.registry`) finds plugins through their entry points: the checkouts in
  `plugins/` first, then installed packages; the same plugin is never loaded twice. Which plugins a
  process loads is a selection (`LIMINAL_PLUGINS_LOADED`, the integrator's `--plugins`): every
  plugin found by default, the ones named, or none.

## How a plugin plugs in

The contract is `liminal_libs/sources/plugin.py` (plugin API 2.0): `Plugin` → `IntegratorPlugin`
(`FeedPlugin`, `PublisherPlugin`: the stock-data plugins) and `PlatformPlugin` (`AssistantPlugin`:
the AI plugins), each named for the system it plugs into. A plugin is an instance of a subclass: what
it declares are class attributes (its name, keys, plans, assets, delays, databases), what it does are
methods overriding the contract's defaults (`build`, `view`, `scan`, `settings`, a publisher's
`moments`/`calendar`/`read`, an assistant's models and evaluations). The record form (`Provider`,
`Publisher`, plugin API 1.x) still loads. A plugin fetches on its own through the shared queue when it
has a `Fetcher`, keeps its own databases in the data folder (`layout.Database`), and refreshes its
own plan information with its capability scan.

## How the programs talk at run time

- Everything shares the databases in `store/data/`; one process at a time owns a writer, through the
  resident hosts of `liminal_libs.host`: a process that runs only while a client is attached over a
  local pipe (named for the host and the database, this user's key on both ends). The hosts are the
  fetcher (one per database), each feed's run (`feed-<name>`), the closed months' copy to DuckDB, the
  live recorder and the keeper of the MongoDB server. A host nobody holds ends after its idle time.
- The charts app's front attaches to the fetcher when a page comes (starting one if none answers) and
  proxies its pages' data requests to the fetcher's data API (port 8785 by default). The integrator's
  service keeps the fetcher's own page there from your logon; `--daemon` holds the fetcher for good
  so it collects on its own. Several integrators can run at once (`--name`), each on its own database
  and port; a key's machine-wide ledger is shared by key, whichever integrator spends it.
- An AI evaluation runs in the caller's process over HTTPS and is recorded in the main file's
  `evaluations` table; nothing is sent from an offline copy.

## Keys and accounts

Keys live only in your user environment variables (the Windows registry's user `Environment` key;
never a file this software writes), declared by each plugin as `KeyVar`s and set from the Plugins view
or `tools/set-key.ps1` in liminal-charts; a plugin's plan is read from its own variables, or found by
its scan. The direction (the user, 2026-11-03): `liminal-login` becomes the root authorization for
all plugins and software integration, providing the key and account infrastructure to the plugins.
Every plugin and software component inherits a login base class that gives it IPC with the running
login app; the login app stores the keys and accounts as key–vector pairs that interrogate the
current logged-in user for a particular account (the key `claude-account-name` or
`claude-account-api-key`; the vector returned is the API key, the account name or the account
password), and only the liminal-login instance can furnish them.

## Branches, versions, releases

Every repository is on `release/0.2.0` for the object-oriented rewrite; the charts app pins the
library, the integrator and the eighteen plugins by that branch. The cut-over sets the library and the
integrator to 0.2.0, moves every pin to the `v0.2.0` tags, merges to `main`, tags and releases each
repository, and the plugins' CI goes back to testing against `main`. Push order when a change crosses
repositories: the library, then the plugins, then the integrator, then the charts app (whose CI installs
all of them). Each repository runs its tests on Windows and Ubuntu over several Python versions.
