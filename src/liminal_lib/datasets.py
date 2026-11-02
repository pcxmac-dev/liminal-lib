"""Research datasets and reference sets: free public files, downloaded whole and kept locally.

Unlike the feeds these are not price bars of watched symbols: factor and portfolio returns,
market-level history, futures positioning, commodity prices, index membership with sector
classes. Each file is fetched once; a file that will not change again (a finished year, an
archive) is then closed and never fetched again, the others refresh on their own cadence (only
once they have been fetched the first time with `datasets fetch`). Each provider's values are kept
in a file of their own (datasets/<provider>.db), and each dataset has a quality prior by provenance
(an official publisher, the researcher's own library, or a mirror of someone else's data).

Every dataset is declared by the publisher plugin that puts it out (Publisher.datasets, a
sources.dataset.Dataset: its files, parser and publication rule), so a new one is a plugin's:
- Ken French's Data Library (the liminal-plugin-french package): Fama/French 3 and 5 factors, 49 industry
  portfolios; daily and monthly, from 1926 (1963 for 5 factors). Returns in percent.
- Datahub's core datasets on GitHub (liminal-plugin-datahub; the mirrors' submodules stay here, in
  data/github/): the S&P 500's members with GICS sector and
  sub-industry (the market map's sectors), Robert J. Shiller's US stock market data from 1871, and
  the IMF's primary commodity prices 1980-2017 (a frozen mirror: an archive, closed once imported).
- The CFTC's Commitments of Traders (Federal, plugins/federal/cftc.py): the legacy futures-only
  report, weekly from 1986: open interest and commercial / non-commercial / small trader positions.
A dataset's files are read only from its publisher's hosts (Policy.hosts; never a blocked site),
and nothing of it is fetched while its plugin is paused (the Plugins view).

Not included: Norgate and CRSP (paid / licensed), Kaggle datasets (need a Kaggle login).

Point in time (storage/datasets.py keeps every version of every value): a version's new and changed
values become available when the source published that version (a git commit's time, or the
file's Last-Modified date). A file's first import is history published on a schedule, so its
values are dated by the source's publication rule (`Dataset.published`), never later than the
file itself. Datasets hosted on GitHub are git submodules of this repository (data/github/...,
pinned commits) and their whole commit history is imported, each commit a published version; the
other files are downloaded and every distinct version is kept locally with its checksum
(<data>/sources/datasets/<provider>/).
"""
from __future__ import annotations

import csv
import email.utils
import hashlib
import os
import subprocess
import zipfile
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

import requests

from . import config
from .layout import SOURCES, downloads
from .sources.dataset import ET, Dataset, Parsed  # noqa: F401 - Parsed: the stores' and tests' import

USER_AGENT = "liminal/1 (personal research; historical archive)"
# The git submodules of GitHub-hosted datasets: <repository root>/data/github/<owner>/<repo>.
REPOS = Path(config.setting("LIMINAL_DATA_REPOS") or Path(__file__).resolve().parents[2] / "data" / "github")
GIT_HOST = "github.com"  # a dataset's git submodule is fetched from here: its publisher names it as a host


def declared() -> dict[str, Dataset]:
    """Every dataset the publisher plugins declare, by name, in the plugins' order, each with the
    publisher declaring it (Dataset.publisher)."""
    from .sources import publishers

    out: dict[str, Dataset] = {}
    for pub in publishers().values():
        for ds in pub.declared:
            out.setdefault(ds.name, replace(ds, publisher=pub.name))
    return out


class _Declared(Mapping):
    """DATASETS: the declared datasets by name, read from the plugins each time (a plugin installed or
    reloaded shows)."""

    def __getitem__(self, name: str) -> Dataset:
        return declared()[name]

    def __iter__(self) -> Iterator[str]:
        return iter(declared())

    def __len__(self) -> int:
        return len(declared())


DATASETS: Mapping[str, Dataset] = _Declared()


def refusal(ds: Dataset, url: str) -> str | None:
    """Why a dataset may not read this URL (None: it may): HTTPS, never a blocked site, and only its
    publisher's hosts (publications.url_refusal)."""
    from .publications import url_refusal
    from .sources import publishers

    pub = publishers().get(ds.publisher)
    if pub is None:
        return f"no plugin declares {ds.name} here"
    return url_refusal(pub, url)


def stopped(store, ds: Dataset, now: datetime | None = None) -> str | None:
    """Why nothing of a dataset is fetched now (None: it may be): its publisher paused or killed, or
    the plugin it is part of (Federal, for the CFTC's)."""
    from . import killswitch
    from .sources import publishers

    pub = publishers().get(ds.publisher)
    if pub is None:
        return f"no plugin declares {ds.name} here"
    for name in dict.fromkeys(n for n in (pub.name, pub.part_of) if n):
        k = killswitch.killed(store, name, "fetch", now)
        if k:
            return f"{name} is paused" if k.get("by") == "user" else f"{name} is stopped: {k.get('why') or 'killed'}"
    return None


def due_files(store, ds: Dataset, today: date, force: bool = False) -> list[tuple[str, bool]]:
    """Files to fetch now: never-fetched ones, and open ones older than the refresh cadence. A
    final file once imported is closed and never fetched again."""
    state = store.dataset_files(ds.name)
    out = []
    for url, final in ds.files(today):
        s = state.get(url)
        if s is None:
            out.append((url, final))
        elif s["closed"]:
            continue
        elif force or (today - s["fetched_at"].date()).days >= ds.refresh_days:
            out.append((url, final))
    return out


def fetch(store, names: list[str] | None = None, today: date | None = None, force: bool = False,
          session: requests.Session | None = None, log=print, only_started: bool = False) -> dict:
    """Fetch (or refresh) datasets: new file versions or commits are stored as versions. With
    `only_started`, only datasets fetched before (the scheduled sync never starts one by itself).
    Returns {name: versions stored}."""
    today = today or datetime.now(ET).date()
    session = session or requests.Session()
    started = {n for n in declared() if store.dataset_files(n)}
    upgrade_legacy(store, log)
    relink_local_paths(store, log)
    done = {}
    known = declared()
    for name in names or list(known):
        ds = known[name]
        if only_started and name not in started:
            continue
        why = stopped(store, ds)
        if why:
            log(f"{name}: not fetched ({why})")
            continue
        if ds.git and _repo(ds) is not None:
            done[name] = fetch_git(store, ds, today, force, log)
        else:
            done[name] = _fetch_files(store, ds, today, force, session, log)
    return done


def _changed(counts: dict) -> int:
    return sum(counts[k] for k in ("new", "revised", "withdrawn", "joined", "changed", "left"))


def _describe(counts: dict) -> str:
    parts = [f"{counts[k]:,} {k}" for k in ("new", "revised", "withdrawn", "joined", "changed", "left") if counts[k]]
    return ", ".join(parts) or "no changes"


def _quality_prior(store, ds: Dataset) -> None:
    if store.quality(ds.name, "dataset") is None:  # a prior until something measures it
        from . import provenance
        from .composite import PRIORS

        derivation = provenance.record(store, "quality-prior", f"quality:{ds.name}", {"kind": ds.kind, "priors": PRIORS},
                                       {"dataset": ds.name, "licence": ds.licence}, rows=1)
        store.set_quality(ds.name, "dataset", PRIORS[ds.kind], {"basis": "prior", "kind": ds.kind, "licence": ds.licence},
                          derivation)


def _ms_dt(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _published_at(resp) -> datetime | None:
    """The file's Last-Modified time: when the source published this version."""
    raw = (getattr(resp, "headers", None) or {}).get("Last-Modified")
    try:
        return email.utils.parsedate_to_datetime(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _keep(store, ds: Dataset, file: str, blob: bytes, sha: str) -> Path | None:
    """Keep this version of a downloaded file: <data>/sources/datasets/<provider>/<name>.<sha12><ext>."""
    if store.datasets_dir is None:
        return None
    base = store.datasets_dir.parent / downloads(ds.provider)
    f = Path(file)
    dest = base / f"{f.stem}.{sha[:12]}{f.suffix}"
    if not dest.exists():
        base.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
    return dest


def _relative(store, local: Path | None) -> str | None:
    """A kept download's path as the ledger records it: relative to the data folder, posix-style (the
    folder can move: it did when the project was renamed, and a path recorded absolute pointed nowhere)."""
    return local.relative_to(store.datasets_dir.parent).as_posix() if local else None


def local_file(store, url: str, local_path: str | None) -> Path | None:
    """Where a ledger row's kept file is: its path relative to the data folder, or to REPOS for the
    commit of a GitHub dataset (a `git:` url); one recorded absolute (before 2026-11-01) as it is."""
    if not local_path:
        return None
    p = Path(local_path)
    if p.is_absolute():
        return p
    if url.startswith("git:"):
        return REPOS / p
    return store.datasets_dir.parent / p if store.datasets_dir is not None else None


def relink_local_paths(store, log=print) -> int:
    """The ledger's kept-file paths recorded absolute (before 2026-11-01) made relative, so the data
    folder and the repositories can move (the rename moved both): a download's by its place under
    sources/datasets/, a commit's by its dataset's repository and file. Returns the rows changed."""
    marker = tuple(SOURCES.split("/"))
    known, n = declared(), 0
    for name, url, lp in store.dataset_local_paths():
        if not lp or not Path(lp).is_absolute():
            continue
        if url.startswith("git:"):
            ds = known.get(name)
            rel = f"{ds.git[0]}/{ds.git[1]}" if ds is not None and ds.git else None
        else:
            parts = Path(lp).parts
            at = next((i for i in range(len(parts) - 1) if parts[i:i + 2] == marker), None)
            rel = "/".join(parts[at:]) if at is not None else None
        if rel:
            store.set_dataset_local_path(name, url, rel)
            n += 1
    if n:
        log(f"datasets: {n} kept files' paths made relative to the data folder")
    return n


def _fetch_files(store, ds: Dataset, today: date, force: bool, session, log) -> int:
    name, n = ds.name, 0
    single = len(ds.files(today)) == 1  # a file holding the whole dataset: what it drops is withdrawn
    due = list(due_files(store, ds, today, force))
    queue = _FileQueue(store, ds, log)
    queue.plan(due)
    for url, final in due:
        file = url.rsplit("/", 1)[-1]
        why = refusal(ds, url)
        if why:
            log(f"{name}: {file} not fetched ({why})")
            queue.end(url, "failed", error=f"not fetched ({why})")
            continue
        queue.start(url)
        try:
            resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=120)
            if resp.status_code == 404 and not final:
                log(f"{name}: {file} not published yet")
                queue.end(url, "done", detail="not published yet", requests=1)
                continue
            resp.raise_for_status()
            parsed = ds.parse(url, resp.content)
        except (requests.RequestException, ValueError, KeyError, zipfile.BadZipFile) as e:
            log(f"{name}: {file} not fetched ({e}); tried again next time")  # the others go on
            queue.end(url, "failed", error=str(e)[:300], requests=1)
            continue
        if not parsed.values and not parsed.constituents:
            log(f"{name}: {file} had nothing to read (format changed?); not marked fetched")
            queue.end(url, "failed", error="nothing to read (format changed?)", requests=1, bytes_=len(resp.content))
            continue
        blob = resp.content
        sha = hashlib.sha256(blob).hexdigest()
        prev = store.dataset_files(name).get(url)
        if prev and prev["sha256"] == sha:
            store.set_dataset_file(name, url, len(blob), prev["values"] or 0, closed=final, sha256=sha,
                                   version_at=prev["version_at"], origin=sha, local_path=prev["local_path"])
            log(f"{name}: {file} unchanged")
            queue.end(url, "done", detail="unchanged", requests=1, bytes_=len(blob))
            continue
        version = _published_at(resp) or datetime.now(timezone.utc)
        local = _keep(store, ds, file, blob, sha)
        counts = store.save_dataset_version(ds.provider, name, parsed, _ms_dt(version), sha,
                                            first_rule=ds.published if prev is None else None,
                                            withdraw=single and prev is not None)
        store.set_dataset_file(name, url, len(blob), _changed(counts), closed=final, sha256=sha, version_at=version,
                               origin=sha, local_path=_relative(store, local))
        _quality_prior(store, ds)
        log(f"{name}: {file} ({len(blob) / 1e6:.2f} MB, published {version:%Y-%m-%d}): {_describe(counts)}"
            + ("; final, closed" if final else ""))
        queue.end(url, "done", detail=_describe(counts), requests=1, bytes_=len(blob), rows=_changed(counts))
        n += 1
    return n


class _FileQueue:
    """A dataset's files on the fetch queue (storage/queue.py; the Fetcher shows it), under its plugin
    and the publisher that declares it (the CFTC's: Federal's, part cftc): each due file waiting, then
    running, done or failed. Never the fetch's undoing: a queue write that fails is logged once."""

    def __init__(self, store, ds: Dataset, log):
        from .sources.registry import publisher_of

        self.store, self.ds, self.log, self.broken = store, ds, log, False
        self.part = ds.publisher or ds.provider
        self.plugin = publisher_of(self.part)

    def _do(self, method: str, *args, **kw) -> None:
        if self.broken:
            return
        try:
            getattr(self.store, method)(*args, **kw)
        except Exception as e:  # noqa: BLE001 - the queue is the view's; the fetch goes on
            self.broken = True
            self.log(f"{self.ds.name}: the fetch queue was not kept ({type(e).__name__}: {e})")

    def title(self, url: str) -> str:
        return f"{self.ds.name}: {url.rsplit('/', 1)[-1]}"

    def plan(self, due: list[tuple[str, bool]]) -> None:
        self._do("queue_put", self.plugin, self.part, "dataset file",
                 [{"key": url, "title": self.title(url), "priority": i, "detail": self.ds.title} for i, (url, _) in enumerate(due)])

    def start(self, url: str) -> None:
        self._do("queue_start", self.plugin, self.part, "dataset file", url, self.title(url))

    def end(self, url: str, state: str, **kw) -> None:
        self._do("queue_end", self.plugin, self.part, "dataset file", url, state, title=self.title(url), **kw)


class GitError(RuntimeError):
    pass


def _repo(ds: Dataset) -> Path | None:
    """The dataset's git submodule checkout, if it is there."""
    if not ds.git:
        return None
    path = REPOS / ds.git[0]
    return path if (path / ".git").exists() else None


def _git(repo: Path, *args: str, binary: bool = False):
    from .proc import no_window

    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=600, **no_window())
    if r.returncode != 0:
        raise GitError(r.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return r.stdout if binary else r.stdout.decode()


def _git_ref(repo: Path) -> str:
    for ref in ("origin/HEAD", "origin/main", "origin/master"):
        try:
            _git(repo, "rev-parse", "--verify", "--quiet", ref)
            return ref
        except GitError:
            continue
    return "HEAD"


def fetch_git(store, ds: Dataset, today: date, force: bool = False, log=print, update: bool = True) -> int:
    """Import every commit of a GitHub-hosted dataset's file not imported yet, oldest first, each a
    published version at its commit time; then check the submodule out at the repository's newest
    commit."""
    repo, path = _repo(ds), ds.git[1]
    files = store.dataset_files(ds.name)
    git_files = {u: f for u, f in files.items() if u.startswith("git:")}
    if git_files and not force and (today - max(f["fetched_at"] for f in git_files.values()).date()).days < ds.refresh_days:
        return 0
    why = refusal(ds, f"https://{GIT_HOST}/{ds.git[0]}") if update else None
    if why:
        log(f"{ds.name}: not updated from {GIT_HOST} ({why}); using the commits already here")
    elif update:
        try:
            _git(repo, "fetch", "--quiet", "origin")
        except GitError as e:
            log(f"{ds.name}: could not update {ds.git[0]} ({e}); using the commits already here")
    ref = _git_ref(repo)
    commits = [line.split(" ", 1) for line in _git(repo, "log", "--reverse", "--format=%H %cI", ref, "--", path).splitlines()
               if line.strip()]
    if not git_files and files:
        store.clear_dataset_files(ds.name)  # the downloads' ledger gives way to the commit history
    seen = {f["origin"] for f in git_files.values()}
    n, totals = 0, dict(new=0, revised=0, withdrawn=0, unchanged=0, joined=0, changed=0, left=0)
    for sha, when in commits:
        if sha in seen:
            continue
        t = datetime.fromisoformat(when)
        blob = _git(repo, "show", f"{sha}:{path}", binary=True)
        try:
            parsed = ds.parse(path, blob)
        except (ValueError, KeyError, csv.Error) as e:
            log(f"{ds.name}: commit {sha[:8]} not readable ({e}); skipped")
            parsed = Parsed()
        url = f"git:{ds.git[0]}@{sha}"
        if not parsed.values and not parsed.constituents:
            store.set_dataset_file(ds.name, url, len(blob), 0, closed=True, version_at=t, origin=sha,
                                   local_path=f"{ds.git[0]}/{path}")
            continue
        counts = store.save_dataset_version(ds.provider, ds.name, parsed, _ms_dt(t), sha,
                                            first_rule=ds.published if not seen and n == 0 else None, withdraw=True)
        store.set_dataset_file(ds.name, url, len(blob), _changed(counts), closed=True, version_at=t, origin=sha,
                               local_path=f"{ds.git[0]}/{path}")
        totals = {k: totals[k] + counts[k] for k in totals}
        n += 1
    if commits:
        _git(repo, "checkout", "--quiet", "--detach", ref)  # the repository's newest commit: the file's latest version
        _quality_prior(store, ds)
    log(f"{ds.name}: {n} new commits of {ds.git[0]} ({len(commits)} in all)" + (f": {_describe(totals)}" if n else ""))
    return n


def upgrade_legacy(store, log=print) -> None:
    """Carry values stored before versioning into versioned storage: datasets from GitHub are
    imported again from their commit history (exact publication times); the others become one
    version dated by their publication rule."""
    for provider in store.dataset_providers():
        obs, members = store.dataset_legacy(provider)
        if not obs and not members:
            continue
        for ds in (d for d in declared().values() if d.provider == provider):
            if _repo(ds) is not None:
                store.clear_dataset_files(ds.name)  # the next fetch imports the commits
                continue
            files = store.dataset_files(ds.name)
            when = max((f["fetched_at"] for f in files.values()), default=datetime.now(timezone.utc))
            parsed = Parsed(values=[(s_, ts, v) for d, s_, ts, v in obs if d == ds.name],
                            constituents=[{"index": i, "ticker": t, "name": n, "sector": se, "industry": ind,
                                           "added": a, "cik": c} for i, t, n, se, ind, a, c in members]
                            if ds.index else [])
            if parsed.values or parsed.constituents:
                store.save_dataset_version(provider, ds.name, parsed, _ms_dt(when), "before-versioning",
                                           first_rule=ds.published)
        store.drop_dataset_legacy(provider)
        log(f"datasets: {provider} carried over to versioned storage")


# -- reading, for the chart app's dataset viewer ---------------------------------------------

MAX_SERIES = 8  # series drawn at once (the categorical palette's eight slots)


def series_list(store, name: str) -> list[dict]:
    """A dataset's series with their span; for the COT report its markets instead (see cot_markets)."""
    ds = DATASETS[name]
    return [{"series": r["series"], "title": r["title"], "unit": r["unit"], "freq": r["freq"],
             "first": r["first_ts"], "last": r["last_ts"], "n": r["n"]}
            for r in store.dataset_series(ds.provider, name) if r["n"]]


def cot_markets(store) -> list[dict]:
    """Markets of the COT report: code, name, span and latest open interest; those still reported
    first, largest open interest first."""
    ds = DATASETS["cftc-cot-legacy"]
    last = store.dataset_last_values(ds.provider, ds.name)
    markets: dict[str, dict] = {}
    for r in store.dataset_series(ds.provider, ds.name):
        code, _, field = r["series"].partition(":")
        m = markets.setdefault(code, {"code": code, "name": code, "first": None, "last": None, "openInterest": None})
        if field == "open_interest":
            m["name"] = (r["title"] or code).rsplit(": ", 1)[0]
            m["first"], m["last"] = r["first_ts"], r["last_ts"]
            m["openInterest"] = last.get(r["series"])
    newest = max((m["last"] or 0 for m in markets.values()), default=0)
    current = newest - 21 * 86_400_000  # reported within three weeks of the newest report
    # still reported first (the comparison whole: `m["last"] or 0 >= current` read `m["last"] or (0 >= current)`)
    return sorted(markets.values(), key=lambda m: (-int((m["last"] or 0) >= current), -(m["openInterest"] or 0), m["name"]))


def values(store, name: str, series: list[str], as_of: int | None = None) -> dict[str, dict]:
    """{series: {title, unit, freq, t: [ms...], v: [values...]}} for up to MAX_SERIES series: the
    latest version, or what was public at `as_of` (ms)."""
    ds = DATASETS[name]
    meta = {r["series"]: r for r in store.dataset_series(ds.provider, name)}
    out = {}
    for s in series[:MAX_SERIES]:
        if s not in meta:
            raise KeyError(f"no series {s!r} in {name}")
        df = store.dataset_values(ds.provider, name, s, as_of=as_of)
        out[s] = {"title": meta[s]["title"], "unit": meta[s]["unit"], "freq": meta[s]["freq"],
                  "t": [int(t) for t in df["ts"]], "v": [float(v) for v in df["value"]]}
    return out


def status(store, today: date | None = None) -> list[dict]:
    """Every dataset: files fetched and closed, values, series, span, last fetch, files due, quality."""
    today = today or datetime.now(ET).date()
    out = []
    from .sources import providers, publishers
    from .sources.registry import publisher_of

    pubs = publishers()
    for ds in declared().values():
        plugin = publisher_of(ds.publisher)  # the plugin it is in: Federal, for the CFTC's
        owner = pubs.get(plugin) or providers().get(plugin)
        files = store.dataset_files(ds.name)
        series = [x for x in store.dataset_series(ds.provider, ds.name) if x["n"]] if files else []  # withdrawn left out
        members = len(store.constituents(ds.provider, ds.index)) if files and ds.index else 0
        q = store.quality(ds.name, "dataset")
        out.append({
            "name": ds.name, "title": ds.title, "provider": ds.provider, "kind": ds.kind, "licence": ds.licence,
            "plugin": plugin, "pluginTitle": owner.title if owner else plugin,
            "part": pubs[ds.publisher].title if plugin != ds.publisher and ds.publisher in pubs else None,
            "files": len(files), "closed": sum(f["closed"] for f in files.values()),
            "bytes": sum(f["bytes"] or 0 for f in files.values()),
            "values": sum(s["n"] or 0 for s in series), "series": len(series), "members": members,
            "first": min((s["first_ts"] for s in series if s["first_ts"] is not None), default=None),
            "last": max((s["last_ts"] for s in series if s["last_ts"] is not None), default=None),
            "fetched": max((f["fetched_at"] for f in files.values()), default=None),
            "due": (int((today - max(f["fetched_at"] for f in files.values()).date()).days >= ds.refresh_days)
                    if ds.git and _repo(ds) is not None else len(due_files(store, ds, today))) if files else None,
            "source": f"git {ds.git[0]}" if ds.git and _repo(ds) is not None else "download",
            "quality": q["score"] if q else None,
        })
    return out
