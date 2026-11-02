"""Stores: which engine keeps each kind of a plugin's data, and the Store picker.

The user, 2026-10-31: "each module needs to support sqlite or mongo or duck db based on data type";
"lets have a 'store' picker so i can choose manually, but lets try to use the optimal data store for
the data type". Each data type lists the engines it can be kept in, the optimal first:
- bars: DuckDB (the open months in SQLite, where the feeds write; the closed ones in DuckDB, read by
  column: storage/history.py) or SQLite alone;
- documents (an agency's releases and rules, a vendor's headlines; filings and records as they come):
  MongoDB (installed here, mongo.py) or SQLite (storage/documents.py);
- series (a dataset's): SQLite (small series changed in place);
- files (the downloads as downloaded): a folder.
A plugin's choice per data type is its pick (the Plugins view), or, left to the app, the optimal one
available here. Where the data is follows the choice: bars a closed month at a time (the hourly
sync's `history run`), documents at once (settle: copied, counted, then read and written there).
What the store left holds stays ("we should always keep all source data"): only the user removes it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .storage import documents, history

ENGINE_TITLES = {"sqlite": "SQLite", "duckdb": "DuckDB", "mongodb": "MongoDB", "files": "Folder"}


@dataclass(frozen=True)
class DataType:
    name: str
    title: str
    engines: tuple[str, ...]  # the engines it can be kept in, the optimal first
    why: str  # why the first is the optimal one
    held: dict  # what each engine means for it


TYPES = {t.name: t for t in (
    DataType("bars", "Bars", ("duckdb", "sqlite"),
             "closed months by column: on Massive's September 2026, 22% of SQLite's size and market-wide scans "
             "over 100 times faster; the open months stay in SQLite, where the feeds write",
             {"duckdb": "open months in SQLite, closed months in DuckDB", "sqlite": "every month in SQLite"}),
    DataType("documents", "Documents", ("mongodb", "sqlite"),
             "documents of uneven shape with their attachments (releases and their PDFs, filings, records)",
             {"mongodb": "a collection on the MongoDB server here", "sqlite": "a table in its SQLite file"}),
    DataType("series", "Series", ("sqlite",), "small series changed in place, read a series at a time",
             {"sqlite": "its dataset's SQLite file"}),
    DataType("files", "Files", ("files",), "kept exactly as downloaded, every version",
             {"files": "a folder of the downloads"}),
)}


def available(engine: str) -> tuple[bool, str]:
    """Whether an engine can be used here, and if not why."""
    if engine in ("sqlite", "files"):
        return True, ""
    if engine == "duckdb":
        return (True, "") if history.available() else (False, "duckdb is not installed in the app's Python")
    if engine == "mongodb":
        from . import mongo

        try:
            import pymongo  # noqa: F401
        except ImportError:
            return False, "pymongo is not installed in the app's Python"
        return (True, "") if mongo.binary() else (False, "the MongoDB server is not installed here")
    return False, f"no engine {engine!r}"


def optimal(dtype: str) -> str:
    """The data type's best engine available here."""
    return next((e for e in TYPES[dtype].engines if available(e)[0]), TYPES[dtype].engines[-1])


def _pick_key(plugin: str, dtype: str) -> str:
    return f"store:pick:{plugin}:{dtype}"


def _effect_key(plugin: str, dtype: str) -> str:
    return f"store:effect:{plugin}:{dtype}"


def picked(store, plugin: str, dtype: str) -> str | None:
    """The engine picked for a plugin's data type (None: left to the app)."""
    v = store.kv_get(_pick_key(plugin, dtype))
    return v or None


def wanted(store, plugin: str, dtype: str) -> str:
    """Where a plugin's data type should be: its pick if usable here, else the optimal."""
    p = picked(store, plugin, dtype)
    return p if p in TYPES[dtype].engines and available(p)[0] else optimal(dtype)


def effects(conn) -> dict[str, str]:
    """Each plugin's documents store in effect, from a main file's rows (any SQLite connection to it:
    the recorder's gov.db reader keeps one of its own)."""
    return {k.split(":")[2]: v for k, v in conn.execute(
        "SELECT key, value FROM kv WHERE key LIKE 'store:effect:%:documents'") if v}


def moved_to(conn, engine: str) -> list[str]:
    """The plugins whose documents are in `engine` now (markets.py: a vendor's headlines by its name)."""
    return [p for p, e in effects(conn).items() if e == engine]


def in_effect(store, plugin: str, dtype: str) -> str:
    """Where it is read and written now. Bars: the choice (their closed months move a run at a
    time, each read from DuckDB once copied); documents: where they were last settled (SQLite until
    a move to MongoDB finished)."""
    if dtype == "documents":
        return store.kv_get(_effect_key(plugin, dtype)) or "sqlite"
    return wanted(store, plugin, dtype)


# -- a plugin's data types --------------------------------------------------------------------

def plugin_types(name: str) -> list[str]:
    """The data types a plugin keeps: a feed plugin's bars, a publisher's documents (an agency's
    items, a vendor's headlines), a dataset's series and files."""
    from .sources.registry import plugin_of, providers, publisher_parts, publishers

    ps, pubs = providers(), publishers()
    out = []
    parts = [n for n in (name, *publisher_parts(name)) if n in pubs]
    if name in ps and plugin_of(name) == name:
        out.append("bars")
    if any(pubs[n].feeds for n in parts):
        out.append("documents")
    if any(pubs[n].declared for n in parts):
        out += ["series", "files"]
    return out


def document_scopes(name: str) -> list[tuple[documents.Kind, dict]]:
    """A plugin's documents: each kind with the documents of it that are the plugin's (`where`).
    An agency's items by its feeds' keys (gov.db's items); a vendor's headlines by the plugin's name
    (its read saves them under it, the main file's news)."""
    from .sources.registry import publisher_parts, publishers

    pubs = publishers()
    parts = [pubs[n] for n in (name, *publisher_parts(name)) if n in pubs]
    keys = [f.key for p in parts if p.read is None for f in p.feeds]
    out = []
    if keys:
        out.append((documents.GOV_ITEMS, {"source": keys}))
    if any(p.read is not None for p in parts):
        out.append((documents.NEWS, {"source": [name]}))
    return out


def item_plugin(source: str) -> str | None:
    """The plugin an agency item's source (a feed's key) is of."""
    from .sources.registry import publisher_of, publishers

    for p in publishers().values():
        if any(f.key == source for f in p.feeds):
            return publisher_of(p.name)
    return None


# -- documents' backends ----------------------------------------------------------------------

def backend(engine: str, kind: documents.Kind, main_conn=None, gov_conn=None, data_dir: Path | None = None,
            start: bool = False):
    """A kind's documents in an engine: SQLite (the main file's news, gov.db's items) or MongoDB.
    With `start` (a write, a move) the server is started here if it is not running; a read never
    waits for a start (the chart app reads the news every few seconds: it falls back to SQLite)."""
    if engine == "mongodb":
        from . import mongo

        if start and data_dir is not None and not mongo.alive():
            mongo.ensure(data_dir)
            mongo.alive(0)
        return documents.MongoDocuments(mongo.database(), kind)
    conn = gov_conn if kind is documents.GOV_ITEMS else main_conn
    if conn is None:
        raise RuntimeError(f"no SQLite file for {kind.name}")
    return documents.SqliteDocuments(conn, kind)


def settle(store, gov_conn, data_dir: Path, log: Callable[[str], None] = lambda s: None,
           only: str | None = None) -> list[dict]:
    """Move each plugin's documents to the engine wanted (its pick, or the optimal): every document
    copied, counted, then the plugin's reads and writes go there. Documents written to the store
    left by a process from before (one not restarted yet) are copied on each pass. Nothing is
    removed from where they were. Returns what moved."""
    from .sources.registry import plugin_of, providers, publisher_of, publishers

    names = [n for n in providers() if plugin_of(n) == n] + [n for n in publishers() if publisher_of(n) == n]
    done = []
    for name in dict.fromkeys(names):
        if only and name != only:
            continue
        scopes = document_scopes(name)
        if not scopes:
            continue
        want, now = wanted(store, name, "documents"), in_effect(store, name, "documents")
        others = [e for e in TYPES["documents"].engines if e != want]
        try:
            if want == "mongodb" and not _mongo_up():  # started here; not starting: said in one line a pass
                from . import mongo

                mongo.ensure(data_dir)
                if not mongo.alive(0):
                    raise RuntimeError("the MongoDB server is not running (python -m liminal mongo start)")
            moved = 0
            for kind, where in scopes:
                dst = backend(want, kind, store.conn, gov_conn, data_dir, start=True)
                for e in others:
                    if e == "mongodb" and not (available(e)[0] and (now == "mongodb" or _mongo_up())):
                        continue
                    src = backend(e, kind, store.conn, gov_conn)
                    read, new = documents.copy(src, dst, where)
                    moved += new
                    if want != now and dst.count(where) < src.count(where):
                        raise RuntimeError(f"{kind.name}: {dst.count(where)} in {want}, {src.count(where)} in {e}")
            if want != now:
                store.kv_set(_effect_key(name, "documents"), want)
                log(f"stores: {name}'s documents now in {ENGINE_TITLES[want]} ({moved:,} copied from "
                    f"{ENGINE_TITLES[now]}, which keeps its copy)")
            if moved or want != now:
                done.append({"plugin": name, "engine": want, "was": now, "copied": moved})
        except Exception as e:  # noqa: BLE001 - one plugin's store failing is its own
            log(f"stores: {name}'s documents stay in {ENGINE_TITLES[now]}: {type(e).__name__}: {e}")
            if want != now:  # a move that failed; where they are already, nothing to report
                done.append({"plugin": name, "engine": now, "was": now, "copied": 0, "error": str(e)})
    return done


def _mongo_up() -> bool:
    from . import mongo

    return mongo.alive()


def plugin_feed_files(plugin: str) -> list[str]:
    """The feed files a plugin's bars are in: its feeds' (registry.plugin_feeds) and their live
    feeds' (Alpaca's iex), each once."""
    from .sources.registry import plugin_feeds, providers

    ps = providers()
    out: list[str] = []
    for n in plugin_feeds(plugin):
        out += [n, *(x.name for x in ps[n].live if x.name != n)]
    return list(dict.fromkeys(out))


# -- the picker -------------------------------------------------------------------------------

def pick(store, plugin: str, dtype: str, engine: str | None) -> None:
    """Set a plugin's store for a data type (None: the optimal, left to the app). Bars: each of its
    feeds reads its closed months from DuckDB, or not (store:feed:<feed>:bars, read by the Store)."""
    t = TYPES.get(dtype)
    if t is None or dtype not in plugin_types(plugin):
        raise LookupError(f"{plugin} keeps no {dtype}")
    if engine is not None:
        if engine not in t.engines:
            raise ValueError(f"{t.title.lower()} are kept in {', '.join(ENGINE_TITLES[e] for e in t.engines)}")
        ok, why = available(engine)
        if not ok:
            raise RuntimeError(why)
    store.kv_set(_pick_key(plugin, dtype), engine or "")
    if dtype == "bars":
        for feed in plugin_feed_files(plugin):
            store.kv_set(f"store:feed:{feed}:bars", wanted(store, plugin, "bars"))


def bars_feeds(store) -> list[str]:
    """The feeds whose closed months go to DuckDB (their plugin's bars store)."""
    from .sources.registry import plugin_of, providers

    if not available("duckdb")[0]:
        return []
    return [f for p in providers() if plugin_of(p) == p and wanted(store, p, "bars") == "duckdb"
            for f in plugin_feed_files(p) if store.kv_get(f"store:feed:{f}:bars") in (None, "", "duckdb")]


def plugin_stores(store, name: str, gov_conn=None) -> list[dict]:
    """A plugin's data types for its page: each with its engines (the optimal first, each usable
    here or why not), its pick, where it should be and where it is, and how far a move has gone."""
    out = []
    for dtype in plugin_types(name):
        t = TYPES[dtype]
        row = {"type": dtype, "title": t.title, "why": t.why, "optimal": optimal(dtype),
               "picked": picked(store, name, dtype), "wanted": wanted(store, name, dtype),
               "effect": in_effect(store, name, dtype),
               "engines": [{"name": e, "title": ENGINE_TITLES[e], "means": t.held[e], "ok": available(e)[0],
                            "why": available(e)[1]} for e in t.engines]}
        if dtype == "bars":
            row["progress"] = _bars_progress(store, name)
        if dtype == "documents":
            row["counts"] = _document_counts(store, name, gov_conn)
        out.append(row)
    return out


def _bars_progress(store, name: str) -> dict:
    feeds = {}
    for f in plugin_feed_files(name):
        months = store.history_months(f)
        if months or store._bars(f) is not None:
            feeds[f] = {"months": sum(1 for m in months if not m["stale"]), "stale": sum(1 for m in months if m["stale"]),
                        "bytes": sum(m["bytes"] for m in months), "bars": sum(m["bars"] + m["built"] for m in months),
                        "newest": months[0]["month"] if months else None,
                        "oldest": months[-1]["month"] if months else None}
    return feeds


def _document_counts(store, name: str, gov_conn) -> dict:
    """How many of the plugin's documents each engine holds (MongoDB's only while it runs)."""
    out = {}
    for engine in TYPES["documents"].engines:
        if engine == "mongodb" and not (available(engine)[0] and _mongo_up()):
            out[engine] = None
            continue
        n = 0
        try:
            for kind, where in document_scopes(name):
                n += backend(engine, kind, store.conn, gov_conn).count(where)
        except Exception:  # noqa: BLE001 - a count never fails the page
            n = None
        out[engine] = n
    return out


# -- the closed months' copy (storage/history.py): one run at a time, within a budget ---------------
HISTORY_LOCK = "history.lock"  # beside the database: one `history run` at a time
HISTORY_BUDGET_S = 20 * 60  # a `history run`'s seconds of copying


def history_due(settings) -> bool:
    """Whether a `history run` has work: feeds whose closed months go to DuckDB, and no run going."""
    from . import proc
    from .storage import Store

    with Store(settings.db_path) as store:
        feeds = bars_feeds(store)
    return bool(feeds) and proc.holder(settings.db_path.with_name(HISTORY_LOCK)) is None
