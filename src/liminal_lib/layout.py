"""Where the data is: the data folder's files, and the databases each plugin points to (API 1.8).

The data folder is the main file's (LIMINAL_DB, ~/LiminalData/liminal.db). In it:
- liminal.db: the main file, the app's: the watchlist, the ledgers, each feed's rows by its name
  (requests a day, events, the reference it gave), Massive's headlines, the fetched files' ledger;
- feeds/<feed>.db a feed's bars and feeds/<feed>-ticks.db its trades (storage/feeds.py), the
  composite model's in feeds/composite.db (the app's, built from the feeds);
- datasets/<file>.db a dataset file's series (storage/datasets.py; Dataset.provider names it) and
  sources/datasets/<file>/ every version of its downloads, as downloaded (datasets.py);
- gov/gov.db what the publishers read and their reads' record (gov.py, publications.py).

The user, 2026-10-31: "the plugin will point to the databases it stores the data in". Each plugin's
are listed by the contract (registry.plugin_databases): the ones above that its declarations name
(its feeds, live feeds, datasets and publishers), and any its own code writes besides
(Provider.databases, Publisher.databases: Massive's headlines, in the main file's news). A database
server's is named by its address, on this machine only (127.0.0.1, localhost): the user, the same
day: "i think im gonna need mongo for downloading business records/tax filings etc".
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

MAIN = "liminal.db"  # the main file, wherever LIMINAL_DB puts it
FEEDS = "feeds"
DATASETS = "datasets"
SOURCES = "sources/datasets"
GOV = "gov/gov.db"
HISTORY = "history"  # a feed's closed months of bars, in DuckDB (storage/history.py)
MONGO = "mongo"  # the MongoDB server's data, when one runs here (mongo.py)
COMPOSITE = "composite"  # the composite model's feed: the app's, no plugin's
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
# What a database is (the user, 2026-10-31: "lets prep for mongo and duck"): SQLite files today;
# DuckDB files and Parquet folders for closed history read by column; a MongoDB collection for
# documents of uneven shape (filings, releases with their documents); a folder of files as downloaded.
ENGINES = ("sqlite", "duckdb", "parquet", "mongodb", "files")


@dataclass(frozen=True)
class Database:
    """A database a plugin's data is in: a file (a folder of files ends in /) in the data folder, the
    main file (MAIN), or a collection on a database server here (mongodb://127.0.0.1:27017/liminal.filings)."""
    where: str
    holds: str  # what of the plugin's it holds
    shared: bool = False  # the app's or other plugins' too: only the plugin's rows in it are its
    engine: str = ""  # one of ENGINES; left empty, told by its address (engine_of)

    @property
    def server(self) -> bool:
        return "://" in self.where

    @property
    def kind(self) -> str:
        return self.engine or engine_of(self.where)


def engine_of(where: str) -> str:
    """A database's engine by its address: mongodb://, *.duckdb, *.parquet, a folder (files), or
    else a SQLite file."""
    if where.startswith(("mongodb://", "mongodb+srv://")):
        return "mongodb"
    if where.endswith(".duckdb"):
        return "duckdb"
    if where.endswith(".parquet") or where.endswith(".parquet/"):
        return "parquet"
    return "files" if where.endswith("/") else "sqlite"


def bars(feed: str) -> str:
    return f"{FEEDS}/{feed}.db"


def ticks(feed: str) -> str:
    return f"{FEEDS}/{feed}-ticks.db"


def history(feed: str) -> str:
    return f"{HISTORY}/{feed}/"


def dataset_file(provider: str) -> str:
    return f"{DATASETS}/{provider}.db"


def downloads(provider: str) -> str:
    return f"{SOURCES}/{provider}/"


def gov_db(main: Path | str) -> Path:
    return Path(main).parent / GOV


def not_here(db: Database) -> str | None:
    """Why a database is refused: an engine the app has not, a server off this machine (the data
    stays local), or a path out of the data folder. None when it is fine."""
    where = db.where
    if db.kind not in ENGINES:
        return f"{where}: no engine {db.kind!r} here ({', '.join(ENGINES)})"
    if "://" in where:
        if db.kind != "mongodb":
            return f"{where}: a server's address for a {db.kind} database"
        host = urlsplit(where).hostname or ""
        return None if host in LOCAL_HOSTS else f"{where}: a database server off this machine"
    p = Path(where)
    if p.is_absolute() or ".." in p.parts or where.startswith(("/", "\\")):
        return f"{where}: not in the data folder"
    return None


def locate(where: str, main: Path | str) -> Path | None:
    """A database's path here (None: a server's, or an in-memory main file)."""
    if "://" in where or str(main) == ":memory:":
        return None
    return Path(main) if where == MAIN else Path(main).parent / where


def size(where: str, main: Path | str) -> int | None:
    """Its bytes on disk, write-ahead log included; a folder's files summed. None: not made yet, or a
    server's (asked nothing)."""
    p = locate(where, main)
    if p is None or not p.exists():
        return None
    if p.is_dir():
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    wal = p.with_name(p.name + "-wal")
    return p.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
