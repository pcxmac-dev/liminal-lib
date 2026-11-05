"""Resident hosts: a process that runs only while a client is attached to it.

The user, 2026-11-01: nothing of the app runs or keeps a database open when they are not using the
app or a plugin directly; "the plugins should have some sort of resident IPC mechanism that tells
them when the client process is no longer attached, and then presto, the plugin gracefully shuts
down after all data is secured".

A host (the live recorder, the keeper of the MongoDB server, a feed's fetching run) listens on a
local pipe named for the host and the database. A client attaches by connecting and holds the
connection for as long as it uses the host; the host counts its attachments and takes a connection
closing, or its client's process ending (the system closes a dead process's handles, and the id
is looked at as well), as a detach. Once its last client is gone it waits IDLE_SECONDS for another,
then sets its stop event: the hosted work ends, secures its data (its own flush and close), and the
process exits. A client that finds no host starts one (no window, detached from its console) and
attaches when it answers; a host that nobody attaches to within START_SECONDS exits too.

The pipe is this user's: a key under LIMINAL_IPC_DIR (~/.liminal/ipc by default), made once and
readable by its owner alone, authenticates both ends (multiprocessing.connection's challenge).
Nothing leaves this machine: a named pipe on Windows, a socket file in the same folder elsewhere.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
from multiprocessing.connection import AuthenticationError, Client, Listener
from pathlib import Path
from typing import Callable

from . import config
from .proc import pid_alive

IDLE_SECONDS = 60.0  # a host ends this long after its last client detached (LIMINAL_IDLE_SECONDS)
START_SECONDS = 30.0  # a new host waits this long for its first client; a client this long for a host it started
CHECK_SECONDS = 5.0  # how often a host makes sure each client's process is still there
HELLO_SECONDS = 5.0  # a connection that has not said which host it wants by then is dropped
API = 1


def ipc_dir() -> Path:
    return Path(config.setting("LIMINAL_IPC_DIR") or Path.home() / ".liminal" / "ipc")


def key() -> bytes:
    """This user's key, made once: both ends of every pipe prove they hold it."""
    path = ipc_dir() / "key"
    try:
        data = path.read_bytes().strip()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    data = os.urandom(32).hex().encode("ascii")
    tmp = path.with_name(f"key.{os.getpid()}.new")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
    return path.read_bytes().strip()


def _digest(db_path: Path) -> str:
    return hashlib.sha1(str(Path(db_path).resolve()).lower().encode("utf-8")).hexdigest()[:10]


def address(name: str, db_path: Path) -> str:
    """Where the host `name` of this database listens: a pipe (Windows) or a socket file."""
    if sys.platform == "win32":
        return rf"\\.\pipe\liminal-{name}-{_digest(db_path)}"
    return str(ipc_dir() / f"{name}-{_digest(db_path)}.sock")


def family() -> str:
    return "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"


def idle_seconds() -> float:
    try:
        return float(config.setting("LIMINAL_IDLE_SECONDS") or IDLE_SECONDS)
    except ValueError:
        return IDLE_SECONDS


class Host:
    """One resident process: run `work(stop)` while clients are attached (serve)."""

    def __init__(self, name: str, db_path: Path, idle: float | None = None, start: float = START_SECONDS, log=print):
        self.name, self.db_path = name, Path(db_path)
        self.idle = idle_seconds() if idle is None else idle
        self.start_wait = start  # a new host waits this long for its first client
        self.log = log
        self.stop = threading.Event()  # set once the clients are gone (or the work must end): the work ends
        self.attached: dict[int, int] = {}  # each attachment -> its client's process id
        self.reason = ""  # why the host stopped
        self._lock = threading.Lock()
        self._listener = None
        self._ever = False  # whether a client ever attached
        self._idle_since: float | None = None
        self._touched: float | None = None  # when a page last touched this host (touch)
        self._seq = 0
        self.handle: Callable[[dict], object] | None = None  # answers a client's `ask` (a status, say)

    @classmethod
    def start(cls, name: str, db_path: Path) -> None:
        """Start the host `name` as a process of its own: no window, outliving this process, on this
        database (LIMINAL_DB in its environment). The hosts' code is the app's (APP_MODULE)."""
        exe = Path(sys.executable)
        pythonw = exe.with_name("pythonw.exe") if os.name == "nt" and exe.with_name("pythonw.exe").exists() else exe
        flags = 0x00000008 | 0x00000200 | 0x08000000 if os.name == "nt" else 0  # DETACHED | NEW_GROUP | NO_WINDOW
        env = {**os.environ, "LIMINAL_DB": str(db_path)}
        subprocess.Popen([str(pythonw), "-m", APP_MODULE, "host", name], creationflags=flags, close_fds=True, env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def clients(self) -> list[int]:
        with self._lock:
            return sorted(set(self.attached.values()))

    def touch(self) -> None:
        """A page of this host's own is in touch (the fetcher's page, served in its process): it
        counts as a client for the idle time, without a pipe."""
        with self._lock:
            self._touched = time.monotonic()
            self._ever = True

    def held(self) -> bool:
        """Whether a client is attached, or a page was in touch within the idle time."""
        with self._lock:
            return bool(self.attached) or (self._touched is not None and time.monotonic() - self._touched < self.idle)

    def serve(self, work: Callable[[threading.Event], None], secure: Callable[[], None] | None = None,
              handle: Callable[[dict], object] | None = None) -> str:
        """Listen for clients and run `work(stop)` here, until the clients are gone (stop set, work
        returns); then `secure()` (the host's flush and close), listening no more. Returns why it
        stopped. A host already listening for this name: nothing runs here."""
        self.handle = handle
        try:
            self._listener = Listener(address(self.name, self.db_path), family(), authkey=key())
        except OSError as e:
            self.reason = f"another host answers for {self.name} ({e})"
            self.log(f"host {self.name}: {self.reason}")
            return self.reason
        threading.Thread(target=self._accept_loop, name=f"host-{self.name}-accept", daemon=True).start()
        threading.Thread(target=self._watch, name=f"host-{self.name}-watch", daemon=True).start()
        self.log(f"host {self.name}: up for {self.db_path.name} (pid {os.getpid()})")
        try:
            work(self.stop)
        finally:
            self.end(self.reason or "its work ended")
            try:
                self._listener.close()
            except OSError:
                pass
            if secure is not None:
                secure()
        self.log(f"host {self.name}: down ({self.reason})")
        return self.reason

    def end(self, reason: str) -> None:
        """End the work now (the clients are told nothing: they see the pipe close)."""
        if not self.reason:
            self.reason = reason
        self.stop.set()

    # -- clients -------------------------------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self.stop.is_set():
            try:
                conn = self._listener.accept()
            except AuthenticationError:
                continue  # not this user's
            except (OSError, EOFError):
                if self.stop.is_set():
                    return
                time.sleep(0.1)
                continue
            with self._lock:
                self._seq += 1
                n = self._seq
            threading.Thread(target=self._client, args=(conn, n), name=f"host-{self.name}-client-{n}", daemon=True).start()

    def _client(self, conn, n: int) -> None:
        pid = 0
        try:
            if not conn.poll(HELLO_SECONDS):
                return  # said nothing: not a client
            hello = conn.recv()
            if not isinstance(hello, dict) or hello.get("attach") != self.name:
                conn.send({"ok": False, "host": self.name, "why": "not this host"})
                return
            pid = int(hello.get("pid") or 0)
            with self._lock:
                self.attached[n] = pid
                self._ever = True
                self._idle_since = None
            self.log(f"host {self.name}: client {pid} attached ({len(self.attached)} attached)")
            conn.send({"ok": True, "host": self.name, "pid": os.getpid(), "api": API})
            while not self.stop.is_set():
                if conn.poll(CHECK_SECONDS):
                    msg = conn.recv()  # EOFError: detached
                    if msg == "detach":
                        return
                    if isinstance(msg, dict) and "ask" in msg:
                        conn.send(self.handle(msg) if self.handle is not None else {"error": "nothing to ask here"})
                elif pid and not pid_alive(pid):
                    return  # its process is gone (a launcher's handle may hold the pipe open)
        except (EOFError, OSError):
            return  # detached, or its process ended: the system closed its end
        finally:
            with self._lock:
                gone = self.attached.pop(n, None) is not None
                left = len(self.attached)
            try:
                conn.close()
            except OSError:
                pass
            if gone:
                self.log(f"host {self.name}: client {pid} detached ({left} attached)")

    def _watch(self) -> None:
        """Ends the work once the last client has been gone for the idle time (or none came)."""
        while not self.stop.wait(0.25):
            with self._lock:
                touched = self._touched is not None and time.monotonic() - self._touched < self.idle
                attached, ever = len(self.attached) or touched, self._ever
                if attached:
                    self._idle_since = None
                    continue
                if self._idle_since is None:
                    self._idle_since = time.monotonic()
                waited = time.monotonic() - self._idle_since
            if ever and waited >= self.idle:
                self.end(f"no client attached for {self.idle:g} s")
            elif not ever and waited >= self.start_wait:
                self.end(f"no client came within {self.start_wait:g} s")


# -- the clients' side ---------------------------------------------------------------------------
class Attachment:
    """A client's hold on a host: detach() (or leave the `with`) lets it wind down."""

    def __init__(self, name: str, db_path: Path, conn, host_pid: int):
        self.name, self.db_path, self.host_pid = name, Path(db_path), host_pid
        self._conn = conn
        self._lock = threading.Lock()
        self.detached = False

    def alive(self) -> bool:
        """Whether the host is still at the other end."""
        with self._lock:
            if self.detached:
                return False
            try:
                while self._conn.poll(0):
                    self._conn.recv()  # anything unasked for is dropped (the host sends nothing unasked)
            except (EOFError, OSError):
                return False
            return True

    def ask(self, **what) -> object:
        """A question to the host (its handle), and its answer."""
        with self._lock:
            self._conn.send({"ask": True, **what})
            return self._conn.recv()

    def detach(self) -> None:
        with self._lock:
            if self.detached:
                return
            self.detached = True
            try:
                self._conn.send("detach")
            except (OSError, ValueError):
                pass
            try:
                self._conn.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.detach()
        return False

    @classmethod
    def to(cls, name: str, db_path: Path, start: Callable[[], None] | None = None, wait: float = START_SECONDS,
           log=print) -> "Attachment | None":
        """Attach to the host `name` of this database: the one answering, or else one started by
        `start` (None: no host answers and none was started, or the started one never answered)."""
        conn = _connect(name, db_path)
        if conn is None and start is not None:
            start()
            end = time.monotonic() + wait
            while conn is None and time.monotonic() < end:
                time.sleep(0.25)
                conn = _connect(name, db_path)
        if conn is None:
            return None
        try:
            conn.send({"attach": name, "pid": os.getpid(), "api": API})
            if not conn.poll(wait):
                raise EOFError("no answer")
            answer = conn.recv()
            if not (isinstance(answer, dict) and answer.get("ok")):
                raise EOFError(f"refused: {answer}")
        except (EOFError, OSError) as e:
            log(f"host {name}: could not attach ({e})")
            conn.close()
            return None
        return cls(name, db_path, conn, int(answer.get("pid") or 0))


def _connect(name: str, db_path: Path):
    try:
        return Client(address(name, db_path), family(), authkey=key())
    except (OSError, AuthenticationError, EOFError):
        return None


def attach(name: str, db_path: Path, start: Callable[[], None] | None = None, wait: float = START_SECONDS,
           log=print) -> Attachment | None:
    """Attach to the host `name` of this database, the one answering or one started by `start`:
    Attachment.to(...)."""
    return Attachment.to(name, db_path, start, wait, log)


APP_MODULE = "liminal_charts"  # the app's package: `python -m liminal_charts host NAME` runs a host (its cli, cmd_host)


def start_host(name: str, db_path: Path) -> None:
    """Start the host `name` as a process of its own on this database: Host.start(name, db_path)."""
    Host.start(name, db_path)


def attach_or_start(name: str, db_path: Path, log=print) -> Attachment | None:
    """Attach to the host `name`, starting it if none answers."""
    return attach(name, db_path, start=lambda: start_host(name, db_path), log=log)


# -- the hosts every program of Liminal knows --------------------------------------------------
FETCHER = "fetcher"  # the fetcher (liminal-integrator): the requests, the passes, the page and the data API
HISTORY = "history"  # the closed months' copy to DuckDB


def feed_host(name: str) -> str:
    """The host of a feed's run."""
    return f"feed-{name}"


def feed_run(name: str, db_path: Path, log=print) -> Attachment | None:
    """Attach to feed `name`'s run of this database (the host feed-NAME, started if none answers:
    `--source NAME sync --only --fetch-only` under a host, no window). Its pass goes on while
    something holds it; let go, it stops at its next request. None: no answer."""
    return attach_or_start(feed_host(name), db_path, log=log)


def history_run(db_path: Path, log=print) -> Attachment | None:
    """Attach to the `history run` of this database (the host history, started if none answers):
    the closed months' copy, ended after the month in hand once let go."""
    return attach_or_start(HISTORY, db_path, log=log)


def attach_fetcher(db_path: Path, log=print) -> Attachment | None:
    """Attach to this database's fetcher (the host FETCHER), started if none answers."""
    return attach_or_start(FETCHER, db_path, log=log)


def wait_for_fetcher(held: Attachment, log=print, every: float = 2.0) -> None:
    """Wait here while the fetcher has something going on (its status says so). Ctrl+C lets it go."""
    try:
        time.sleep(every)  # its pass starts at its next tick
        while held.alive():
            status = held.ask(status=True)
            if not (isinstance(status, dict) and status.get("busy")):
                break
            time.sleep(every)
    except KeyboardInterrupt:
        held.detach()
        log("sync: the fetcher let go; its runs stop at their next request unless something else holds it")
        raise


def wait_for_requests(db_path: Path, ids: list[int], held: Attachment, log=print, every: float = 2.0) -> None:
    """Wait here until each request of `ids` has ended (done, failed or cancelled), reporting each."""
    from .storage import Store
    from .storage.requests import OPEN

    left = set(ids)
    try:
        while left and held.alive():
            with Store(db_path) as store:
                for rid in sorted(left):
                    r = store.request_get(rid)
                    if r is None or r["state"] not in OPEN:
                        left.discard(rid)
                        if r is not None:
                            log(f"sync: {r['symbol']}: {r['state']} ({r['detail'] or ''})")
            if left:
                time.sleep(every)
    except KeyboardInterrupt:
        held.detach()
        log("sync: the fetcher let go; the requests stay on its stack")
        raise
