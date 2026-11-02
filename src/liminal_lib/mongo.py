"""The MongoDB server here: the documents' store when a plugin's documents are kept in MongoDB (stores.py).

The user, 2026-10-31, approved MongoDB Community Server 9.0.4 (the zip from fastdl.mongodb.org,
mongod.exe unpacked under %LOCALAPPDATA%\\Programs\\MongoDB\\9.0.4, no Windows service) and chose
that the app starts it: like the live recorder, detached and without a window, when a store uses it
(the hourly sync, a store picked in the Plugins view). It listens on 127.0.0.1 only (the data stays
on this machine; never a port forwarded), its WiredTiger cache capped at CACHE_GB (the memory rules:
small caches), its data in the data folder's mongo/ and its log in logs/mongod.log. `python -m
liminal mongo stop` ends it cleanly (the server's own shutdown).
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from . import config
from .layout import MONGO

HOST = "127.0.0.1"
PORT = 27017
CACHE_GB = 0.5
DATABASE = "liminal"  # the app's database on the server: a kind of document is a collection in it
START_WAIT_S = 20.0  # how long a start waits for the server to answer
ALIVE_S = 3.0  # a ping's answer trusted this long (the chart app asks on every news read)
_clients: dict[int, object] = {}
_lock = threading.Lock()
_alive: list = [0.0, False]  # when the server was last asked (monotonic s), and whether it answered


def port() -> int:
    try:
        return int(config.setting("LIMINAL_MONGO_PORT") or PORT)
    except ValueError:
        return PORT


def url() -> str:
    return f"mongodb://{HOST}:{port()}/"


def installed() -> bool:
    try:
        import pymongo  # noqa: F401
    except ImportError:
        return False
    return binary() is not None


def binary() -> Path | None:
    """mongod: MONGOD if set, else the newest under %LOCALAPPDATA%\\Programs\\MongoDB\\<version>\\."""
    named = os.environ.get("MONGOD")
    if named:
        return Path(named) if Path(named).is_file() else None
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Programs" / "MongoDB"
    found = [p for p in root.glob("*/mongod.exe")] + [p for p in root.glob("*/bin/mongod.exe")] if root.is_dir() else []

    def version(p: Path) -> tuple:
        name = p.parent.name if p.parent.name != "bin" else p.parent.parent.name
        return tuple(int(x) if x.isdigit() else 0 for x in name.split("."))
    return max(found, key=version) if found else None


def version() -> str | None:
    b = binary()
    if b is None:
        return None
    return b.parent.name if b.parent.name != "bin" else b.parent.parent.name


def client(timeout_ms: int = 2000):
    """A client for this process (pymongo pools its connections), on 127.0.0.1 only."""
    from pymongo import MongoClient

    with _lock:
        c = _clients.get(port())
        if c is None:
            c = _clients[port()] = MongoClient(HOST, port(), serverSelectionTimeoutMS=timeout_ms, connectTimeoutMS=timeout_ms,
                                               directConnection=True, appname="liminal")
        return c


def database():
    return client()[DATABASE]


def running(timeout_ms: int = 500) -> bool:
    """Whether a server answers here (a ping)."""
    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError
    except ImportError:
        return False
    c = MongoClient(HOST, port(), serverSelectionTimeoutMS=timeout_ms, connectTimeoutMS=timeout_ms, directConnection=True)
    try:
        c.admin.command("ping")
        return True
    except PyMongoError:
        return False
    finally:
        c.close()


def alive(max_age_s: float = ALIVE_S) -> bool:
    """Whether the server answers, asked at most every `max_age_s` (0: asked now). A read of the
    documents checks this, never a fresh ping each: a ping is a client of its own, with its threads."""
    now = time.monotonic()
    if max_age_s and now - _alive[0] < max_age_s:
        return _alive[1]
    _alive[:] = [now, running()]
    return _alive[1]


def args(data_dir: Path) -> list[str]:
    """mongod's command line: this machine only, the cache capped, the data and the log in the data folder."""
    return [str(binary()), "--dbpath", str(data_dir / MONGO), "--bind_ip", HOST, "--port", str(port()),
            "--wiredTigerCacheSizeGB", str(CACHE_GB), "--logpath", str(data_dir / "logs" / "mongod.log"), "--logappend",
            "--setParameter", "diagnosticDataCollectionEnabled=false"]


def _spawn(argv: list[str]) -> int:
    """Start mongod detached, without a window, outliving this process. Returns its id."""
    flags = 0x00000008 | 0x00000200 | 0x08000000 if os.name == "nt" else 0  # DETACHED | NEW_GROUP | NO_WINDOW
    return subprocess.Popen(argv, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid


def start(data_dir: Path, wait_s: float = START_WAIT_S) -> int | None:
    """Start the server unless one answers (a start lock keeps two processes from starting it at
    once). Returns the new process's id, or None if it was running. Raises if it cannot start."""
    from . import proc

    if running():
        return None
    startable()
    lock = data_dir / "mongo-start.lock"
    if not proc.acquire(lock):  # another process is starting it: wait for it to answer
        return None if _answers(wait_s) else _fail(data_dir)
    try:
        if running():
            return None
        (data_dir / MONGO).mkdir(parents=True, exist_ok=True)
        (data_dir / "logs").mkdir(parents=True, exist_ok=True)
        pid = _spawn(args(data_dir))
        return pid if _answers(wait_s) else _fail(data_dir)
    finally:
        proc.release(lock)


def startable() -> None:
    """Raises when the server cannot be started from here: a reading copy, or not installed."""
    from .storage.common import read_only

    if read_only():
        raise RuntimeError("read-only (LIMINAL_READONLY): MongoDB is not started from here")
    if binary() is None:
        raise RuntimeError("MongoDB is not installed here (mongod.exe under %LOCALAPPDATA%\\Programs\\MongoDB)")


def _answers(wait_s: float) -> bool:
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if running():
            _alive[:] = [time.monotonic(), True]
            return True
        time.sleep(0.5)
    return False


def _fail(data_dir: Path):
    raise RuntimeError(f"MongoDB did not answer on {HOST}:{port()} after starting: see {data_dir / 'logs' / 'mongod.log'}")


# -- the keeper: a resident host (host.py) -----------------------------------------------------
# The server runs under a keeper, `python -m liminal host mongo`: a process whose documents live
# in MongoDB attaches to the keeper (attach, once for the process's life, or until detach), the
# keeper starts the server for its first client, and shuts it down cleanly once its last client
# has been gone for the idle time (the user, 2026-11-01: "attached software like duckdb or what
# ever should be controlled by the plugin"; no processes when the app is not in use). The keeper
# is this data folder's: named for its main database, liminal.db by convention.
KEEPER = "mongo"  # the host's name (host.py)
KEEP_SECONDS = 5.0  # how often the keeper makes sure the server still answers
_attachment = None  # this process's hold on the keeper
_attach_lock = threading.Lock()


def keeper_key(data_dir: Path) -> Path:
    return Path(data_dir) / "liminal.db"


def attach(data_dir: Path, log=print) -> bool:
    """Attach this process to the keeper of this data folder's server, the keeper (and so the
    server) started if none answers. Whether the server answers now. Raises when it cannot be
    started from here (not installed, a reading copy)."""
    global _attachment
    from . import host
    from .storage.common import read_only

    with _attach_lock:
        if _attachment is None or not _attachment.alive():
            if read_only():
                raise RuntimeError("read-only (LIMINAL_READONLY): MongoDB is not started from here")
            if binary() is None:
                if running():
                    return True  # a server somebody else runs: used, not kept
                raise RuntimeError("MongoDB is not installed here (mongod.exe under %LOCALAPPDATA%\\Programs\\MongoDB)")
            _attachment = host.attach_or_start(KEEPER, keeper_key(data_dir), log=log)
            if _attachment is None:
                if running():
                    return True  # the keeper did not come up, the server answers all the same: used
                raise RuntimeError("MongoDB's keeper did not answer (python -m liminal host mongo)")
    return _answers(START_WAIT_S)


def detach() -> None:
    """Let the keeper go (the chart app going quiet): the server stops once every client has."""
    global _attachment
    with _attach_lock:
        held, _attachment = _attachment, None
    if held is not None:
        held.detach()
    _alive[:] = [0.0, False]  # asked again next time


def ensure(data_dir: Path) -> bool:
    """The server answering, this process attached to its keeper (stores.py, before documents are
    read or written there). Returns whether it answers."""
    try:
        return attach(data_dir)
    except RuntimeError:
        return False


def keep(data_dir: Path, stop: threading.Event, log=print) -> None:
    """The keeper's work: the server started (or the one running adopted), kept until the clients
    are gone (`stop`) or it stops answering (`mongo stop` by hand)."""
    pid = start(data_dir)
    log(f"host mongo: server {'started' if pid else 'running already'} on {HOST}:{port()}")
    while not stop.wait(KEEP_SECONDS):
        if not running():
            log("host mongo: the server is gone; the keeper ends")
            return


def stop() -> bool:
    """End the server cleanly (its own shutdown command). Returns whether one was running."""
    from pymongo.errors import AutoReconnect, ConnectionFailure, PyMongoError

    if not running():
        return False
    from pymongo import MongoClient

    c = MongoClient(HOST, port(), serverSelectionTimeoutMS=2000, directConnection=True)
    try:
        c.admin.command("shutdown")
    except (AutoReconnect, ConnectionFailure):
        pass  # it closes the connection as it goes
    except PyMongoError:
        raise
    finally:
        c.close()
    with _lock:
        old = _clients.pop(port(), None)
    if old is not None:
        old.close()
    _alive[:] = [time.monotonic(), False]
    return True


def status(data_dir: Path) -> dict:
    """What the Resources view and `mongo status` show: installed (where, version), running (its
    process, memory, uptime), the app's database's size, the cache cap."""
    out = {"installed": installed(), "binary": str(binary()) if binary() else None, "version": version(),
           "host": HOST, "port": port(), "cacheGB": CACHE_GB, "dataDir": str(data_dir / MONGO),
           "running": False, "pid": None, "residentMB": None, "uptimeS": None, "dataBytes": None,
           "storageBytes": None, "collections": None}
    disk = data_dir / MONGO
    out["diskBytes"] = sum(f.stat().st_size for f in disk.rglob("*") if f.is_file()) if disk.is_dir() else None
    if not out["installed"] or not alive():
        return out
    try:
        c = client()
        s = c.admin.command("serverStatus")
        st = c[DATABASE].command("dbStats")
        out.update(running=True, pid=s.get("pid"), residentMB=(s.get("mem") or {}).get("resident"),
                   uptimeS=s.get("uptime"), serverVersion=s.get("version"), dataBytes=st.get("dataSize"),
                   storageBytes=st.get("storageSize"), collections=st.get("collections"))
    except Exception as e:  # noqa: BLE001 - a status read never fails the view
        out["error"] = f"{type(e).__name__}: {e}"
    return out
