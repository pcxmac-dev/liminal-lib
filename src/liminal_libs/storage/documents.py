"""Documents: what the publishers read (an agency's releases and rules, a vendor's headlines) and, as
they come, filings and records, kept in SQLite or MongoDB by the plugin's documents store (stores.py).

The user, 2026-10-31: "we can use a mongo db ... for mixed media", "i think im gonna need mongo for
downloading business records/tax filings etc", "each module needs to support sqlite or mongo or duck
db based on data type". A kind of document (Kind) has the same fields in either: in SQLite a table's
columns (a list as JSON), in MongoDB a collection's documents, each with its key as `_id`. Both
answer the same calls (put, find, newest, count, every), so a plugin's code does not change with
its store, and a move from one to the other (stores.settle) is a copy of `every` into `put`.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Iterator


@dataclass(frozen=True)
class Kind:
    """A kind of document: its table (SQLite) and collection (MongoDB), the fields making its key,
    the field it is ordered by (newest first), its fields in the table's column order, and those
    kept as JSON in SQLite (lists)."""
    name: str
    table: str
    collection: str
    key: tuple[str, ...]
    order: str
    fields: tuple[str, ...]
    lists: tuple[str, ...] = ()

    def id(self, doc: dict) -> str:
        return ":".join(str(doc[k]) for k in self.key)


# An agency's items (gov.py: gov/gov.db's items) and a vendor's headlines (the main file's news).
GOV_ITEMS = Kind("gov_items", "items", "gov_items", ("source", "id"), "published",
                 ("source", "id", "agency", "title", "url", "published", "kind", "tags", "fetched_at"), ("tags",))
NEWS = Kind("news", "news", "news", ("source", "id"), "published_ms",
            ("source", "id", "published_ms", "title", "publisher", "url", "tickers"), ("tickers",))
KINDS = {k.name: k for k in (GOV_ITEMS, NEWS)}


class SqliteDocuments:
    """A kind's documents in a SQLite table of its fields."""
    engine = "sqlite"

    def __init__(self, conn: sqlite3.Connection, kind: Kind):
        self.conn, self.kind = conn, kind

    def _row(self, doc: dict) -> tuple:
        return tuple(json.dumps(doc.get(f) or []) if f in self.kind.lists else doc.get(f) for f in self.kind.fields)

    def _doc(self, row: tuple) -> dict:
        d = dict(zip(self.kind.fields, row))
        for f in self.kind.lists:
            d[f] = json.loads(d[f] or "[]")
        return d

    def put(self, docs: Iterable[dict], replace: bool = False) -> int:
        """Store documents; one held already is kept (`replace`: replaced). Returns how many were new
        (replaced: how many were given)."""
        rows = [self._row(d) for d in docs]
        with self.conn:
            before = self.conn.total_changes
            self.conn.executemany(f"INSERT OR {'REPLACE' if replace else 'IGNORE'} INTO {self.kind.table} "
                                  f"({', '.join(self.kind.fields)}) VALUES ({', '.join('?' * len(self.kind.fields))})", rows)
            changed = self.conn.total_changes - before
        return len(rows) if replace else changed

    def _where(self, where: dict | None, since) -> tuple[str, list]:
        sql, args = [], []
        for f, v in (where or {}).items():
            if isinstance(v, (list, tuple, set)):
                sql.append(f"{f} IN ({', '.join('?' * len(v))})")
                args += list(v)
            else:
                sql.append(f"{f}=?")
                args.append(v)
        if since is not None:
            sql.append(f"{self.kind.order} >= ?")
            args.append(since)
        return (" WHERE " + " AND ".join(sql)) if sql else "", args

    def find(self, where: dict | None = None, since=None, limit: int | None = None) -> list[dict]:
        """Documents matching `where` (a field's value, or one of a list), from `since` on (by the
        kind's order field), newest first."""
        w, args = self._where(where, since)
        sql = f"SELECT {', '.join(self.kind.fields)} FROM {self.kind.table}{w} ORDER BY {self.kind.order} DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [self._doc(r) for r in self.conn.execute(sql, args)]

    def newest(self, where: dict | None = None):
        w, args = self._where(where, None)
        return self.conn.execute(f"SELECT MAX({self.kind.order}) FROM {self.kind.table}{w}", args).fetchone()[0]

    def count(self, where: dict | None = None) -> int:
        w, args = self._where(where, None)
        return self.conn.execute(f"SELECT COUNT(*) FROM {self.kind.table}{w}", args).fetchone()[0]

    def every(self, where: dict | None = None, batch: int = 5000) -> Iterator[list[dict]]:
        """All documents (matching `where`) a batch at a time, by key."""
        w, args = self._where(where, None)
        cur = self.conn.execute(f"SELECT {', '.join(self.kind.fields)} FROM {self.kind.table}{w} "
                                f"ORDER BY {', '.join(self.kind.key)}", args)
        while rows := cur.fetchmany(batch):
            yield [self._doc(r) for r in rows]


class MongoDocuments:
    """A kind's documents in a MongoDB collection, each with its key as `_id`."""
    engine = "mongodb"

    def __init__(self, db, kind: Kind):
        self.kind = kind
        self.coll = db[kind.collection]
        self._indexed = False

    def _index(self) -> None:
        if not self._indexed:
            self.coll.create_index([(self.kind.order, -1)])
            self.coll.create_index([(self.kind.key[0], 1), (self.kind.order, -1)])
            self._indexed = True

    def put(self, docs: Iterable[dict], replace: bool = False) -> int:
        from pymongo import ReplaceOne, UpdateOne

        docs = [{f: d.get(f) if f not in self.kind.lists else list(d.get(f) or []) for f in self.kind.fields}
                for d in docs]
        if not docs:
            return 0
        self._index()
        ops = [ReplaceOne({"_id": self.kind.id(d)}, {"_id": self.kind.id(d), **d}, upsert=True) if replace else
               UpdateOne({"_id": self.kind.id(d)}, {"$setOnInsert": d}, upsert=True) for d in docs]
        got = self.coll.bulk_write(ops, ordered=False)
        return len(docs) if replace else got.upserted_count

    @staticmethod
    def _filter(where: dict | None, order: str, since) -> dict:
        f = {k: {"$in": list(v)} if isinstance(v, (list, tuple, set)) else v for k, v in (where or {}).items()}
        if since is not None:
            f[order] = {"$gte": since}
        return f

    def _plain(self, d: dict) -> dict:
        return {f: d.get(f) if f not in self.kind.lists else list(d.get(f) or []) for f in self.kind.fields}

    def find(self, where: dict | None = None, since=None, limit: int | None = None) -> list[dict]:
        cur = self.coll.find(self._filter(where, self.kind.order, since)).sort(self.kind.order, -1)
        if limit is not None:
            cur = cur.limit(int(limit))
        return [self._plain(d) for d in cur]

    def newest(self, where: dict | None = None):
        d = self.coll.find_one(self._filter(where, self.kind.order, None), sort=[(self.kind.order, -1)])
        return d.get(self.kind.order) if d else None

    def count(self, where: dict | None = None) -> int:
        return self.coll.count_documents(self._filter(where, self.kind.order, None))

    def every(self, where: dict | None = None, batch: int = 5000) -> Iterator[list[dict]]:
        out = []
        for d in self.coll.find(self._filter(where, self.kind.order, None)).sort("_id", 1).batch_size(batch):
            out.append(self._plain(d))
            if len(out) >= batch:
                yield out
                out = []
        if out:
            yield out


def copy(src, dst, where: dict | None = None) -> tuple[int, int]:
    """Every document of `src` (matching `where`) into `dst` (kept where held: a copy run twice puts
    nothing twice). Returns (documents read, new in dst)."""
    read = new = 0
    for batch in src.every(where):
        read += len(batch)
        new += dst.put(batch)
    return read, new
