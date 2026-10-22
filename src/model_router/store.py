"""SQLite index of the ledger: the same records as the JSON-lines files, in tables that can be queried
across months (`router stats`, the calibration of the start table). The JSON-lines files stay the
source: they only append, they survive a cut, and they rebuild this file at any time (`router db
rebuild`). The index is best effort: a write that fails never stops the task that was just paid for,
it is kept in `errors`, and the next sync rebuilds the file. Every task row also keeps its original
record, so the columns lose nothing, and the only trace of a prompt is its fingerprint: the database
stores no prompt or answer, as the ledger does not; a judge's reason may quote a few words of either.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any  # Any: ledger records are JSON

SCHEMA_VERSION = 4  # 4: one judge per attempt; 3: no calibration table; 2: rows keyed by (source file, task id)
_LOCK = threading.Lock()
LedgerFiles = Iterable[tuple[Path, str | None]]  # (file, strategy name when it is a bench ledger)
SIDECARS = ("-journal", "-wal", "-shm")  # SQLite's companions of the index file
UNREADABLE = (KeyError, TypeError, ValueError, AttributeError)  # valid JSON that is not a record the index can read

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source (
    name TEXT PRIMARY KEY, strategy TEXT, bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL DEFAULT 0,
    lines INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task (
    source TEXT NOT NULL, task_id TEXT NOT NULL, strategy TEXT, fingerprint TEXT, kind TEXT NOT NULL,
    risk TEXT NOT NULL, status TEXT NOT NULL, verify TEXT, started_at TEXT, elapsed REAL, final_cell TEXT,
    retries INTEGER NOT NULL DEFAULT 0, work_cost_usd REAL NOT NULL DEFAULT 0, verify_cost_usd REAL NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0, router_version TEXT, calibration_id TEXT, eval_id TEXT, repeat INTEGER,
    record TEXT NOT NULL, PRIMARY KEY (source, task_id)
);
CREATE INDEX IF NOT EXISTS task_kind ON task(kind, risk);
CREATE INDEX IF NOT EXISTS task_eval ON task(eval_id);
CREATE TABLE IF NOT EXISTS attempt (
    source TEXT NOT NULL, task_id TEXT NOT NULL, n INTEGER NOT NULL, cell TEXT NOT NULL, served_model TEXT,
    stop_reason TEXT, input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0, verdict INTEGER, verdict_source TEXT, reason TEXT, verifier TEXT,
    verify_cost_usd REAL NOT NULL DEFAULT 0, carried_reason INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, task_id, n)
);
CREATE INDEX IF NOT EXISTS attempt_cell ON attempt(cell);
CREATE TABLE IF NOT EXISTS verdict (
    source TEXT NOT NULL, task_id TEXT NOT NULL, n INTEGER NOT NULL, idx INTEGER NOT NULL, cell TEXT NOT NULL,
    role TEXT NOT NULL, passed INTEGER NOT NULL, confidence REAL, reason TEXT,
    PRIMARY KEY (source, task_id, n, idx)
);
CREATE TABLE IF NOT EXISTS override (
    source TEXT NOT NULL, task_id TEXT NOT NULL, at TEXT NOT NULL, passed INTEGER NOT NULL, note TEXT
);
"""
TABLES = ("task", "attempt", "verdict", "override", "source")


def _signature(path: Path) -> tuple[int, int] | None:
    """Return `path`'s size and modification time in nanoseconds, or None when it does not exist."""
    if not path.exists():
        return None
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def _unusable(exc: sqlite3.Error) -> bool:
    """Return whether `exc` says the file is no database at all (not merely locked or busy): worth discarding."""
    return type(exc) is sqlite3.DatabaseError


class Store:
    """The SQLite index at `path`. `record` indexes one ledger record as it is written; `rebuild` reloads
    everything from the ledger files; `task_records` gives the records back in the shape `Ledger.tasks`
    returns, overrides applied, so `summarize` works on either. Rows are keyed by ledger file and task id:
    the same eval task under two bench strategies is two rows.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.errors: list[str] = []  # index writes that failed; the ledger line was written all the same

    def _connect(self) -> sqlite3.Connection:
        """Open the index and create its tables; an index written with another schema version is discarded first.

        Raises:
            sqlite3.DatabaseError: the file is not a database, or it is locked for longer than the timeout.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
            if row is None or row[0] == str(SCHEMA_VERSION):
                conn.executescript(SCHEMA)
                conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
                conn.commit()
                return conn
        except sqlite3.DatabaseError:
            conn.close()
            raise
        conn.close()  # another version's index: start over, the next sync rebuilds it from the ledger files
        self.discard()
        return self._connect()

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection under the process lock: commit on success, roll back on an error, close either way."""
        with _LOCK, closing(self._connect()) as conn, conn:
            yield conn

    def discard(self) -> None:
        """Delete the index file and SQLite's side files; the next sync builds a fresh one from the ledger files."""
        for suffix in ("", *SIDECARS):
            Path(str(self.path) + suffix).unlink(missing_ok=True)

    def record(self, record: dict[str, Any], *, source: Path, strategy: str | None = None) -> None:
        """Index `record` (a task, an override or a bench note) that `source` just appended as one JSON line, and
        remember the file's size so `stale` can tell when something wrote to it without passing through here.
        Never raises for the index's sake: a failure goes to `errors`, a file that is no database is discarded,
        and the next sync rebuilds the index from the ledger files.
        """
        appended = len((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))  # what Ledger wrote
        try:
            with self._open() as conn:
                self._insert(conn, record, source.name, strategy)
                self._touch(conn, source, strategy, appended=appended)
        except (sqlite3.Error, OSError) as exc:
            self.errors.append(f"{self.path}: {exc}")
            if isinstance(exc, sqlite3.Error) and _unusable(exc):
                self.discard()

    def _touch(
        self, conn: sqlite3.Connection, source: Path, strategy: str | None, *, appended: int = 0, lines: int | None = None
    ) -> None:
        """Remember `source`'s size and modification time: the real ones after a rebuild (`lines` given), or after
        a live write only when the file grew by exactly the record just appended; otherwise -1, unknown, so the
        next sync rebuilds.
        """
        size, mtime = _signature(source) or (0, 0)
        row = conn.execute("SELECT strategy, bytes, lines FROM source WHERE name = ?", (source.name,)).fetchone()
        strategy = strategy or (row[0] if row else None)
        if lines is None:
            before = int(row[1]) if row else 0
            known = size if (row is None or before >= 0) and before + appended == size else -1
            lines = (int(row[2]) if row else 0) + 1
        else:
            known = size
        conn.execute("INSERT OR REPLACE INTO source (name, strategy, bytes, mtime_ns, lines) VALUES (?, ?, ?, ?, ?)",
                     (source.name, strategy, known, mtime, lines))

    def _insert(self, conn: sqlite3.Connection, record: dict[str, Any], source: str, strategy: str | None) -> None:
        kind = record.get("type")
        if kind == "override":
            conn.execute("INSERT INTO override (source, task_id, at, passed, note) VALUES (?, ?, ?, ?, ?)",
                         (source, record["task_id"], record.get("at", ""), int(bool(record["passed"])),
                          record.get("note", "")))
        elif kind == "bench":
            conn.execute("INSERT OR REPLACE INTO source (name, strategy, bytes, mtime_ns, lines) VALUES (?, ?, 0, 0, 0)",
                         (source, record.get("strategy"), ))
        elif kind == "task":
            self._insert_task(conn, record, source, strategy)

    def _insert_task(self, conn: sqlite3.Connection, record: dict[str, Any], source: str, strategy: str | None) -> None:
        task_id = record["task_id"]
        if strategy is None:
            row = conn.execute("SELECT strategy FROM source WHERE name = ?", (source,)).fetchone()
            strategy = row[0] if row else None
        conn.execute("DELETE FROM attempt WHERE source = ? AND task_id = ?", (source, task_id))
        conn.execute("DELETE FROM verdict WHERE source = ? AND task_id = ?", (source, task_id))
        conn.execute(
            "INSERT OR REPLACE INTO task (source, task_id, strategy, fingerprint, kind, risk, status, verify, started_at, "
            "elapsed, final_cell, retries, work_cost_usd, verify_cost_usd, total_cost_usd, router_version, calibration_id, "
            "eval_id, repeat, record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source, task_id, strategy, record.get("fingerprint"), record.get("kind", "unknown"),
             record.get("risk", "medium"), record.get("status", "unsolved"), record.get("verify"),
             record.get("started_at"), record.get("elapsed"), record.get("final_cell"), int(record.get("retries") or 0),
             float(record.get("work_cost_usd") or 0.0), float(record.get("verify_cost_usd") or 0.0),
             float(record.get("total_cost_usd") or 0.0), record.get("router_version"), record.get("calibration_id"),
             record.get("eval_id"), record.get("repeat"), json.dumps(record, ensure_ascii=False)),
        )
        for a in record.get("attempts") or []:
            usage = a.get("usage") or {}
            verdict = a.get("verdict")
            conn.execute(
                "INSERT OR REPLACE INTO attempt (source, task_id, n, cell, served_model, stop_reason, input_tokens, "
                "output_tokens, cost_usd, verdict, verdict_source, reason, verifier, verify_cost_usd, "
                "carried_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source, task_id, int(a.get("n") or 0), a.get("cell", "?"), a.get("served_model"), a.get("stop_reason"),
                 int(usage.get("input") or 0), int(usage.get("output") or 0), float(a.get("cost_usd") or 0.0),
                 None if verdict is None else int(bool(verdict)), a.get("verdict_source"), a.get("reason"),
                 a.get("verifier"), float(a.get("verify_cost_usd") or 0.0), int(bool(a.get("carried_reason", False)))),
            )
            for idx, v in enumerate(a.get("verdicts") or []):
                conn.execute(
                    "INSERT OR REPLACE INTO verdict (source, task_id, n, idx, cell, role, passed, confidence, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (source, task_id, int(a.get("n") or 0), idx, v.get("cell", "?"), v.get("role", "judge"),
                     int(bool(v.get("passed"))), v.get("confidence"), v.get("reason")),
                )

    def rebuild(self, ledgers: LedgerFiles) -> dict[str, int]:
        """Drop every row and reload the index from `ledgers`; a file that is no database is replaced first. Lines
        that are not UTF-8, not JSON, or JSON that is not a record are skipped and counted in `corrupt`. Return
        `counts`.
        """
        files = list(ledgers)
        corrupt = 0
        with _LOCK:
            try:
                conn = self._connect()
            except sqlite3.DatabaseError as exc:
                if not _unusable(exc):
                    raise
                self.discard()
                conn = self._connect()
            with closing(conn), conn:
                for name in TABLES:
                    conn.execute(f"DELETE FROM {name}")  # noqa: S608 - table names come from TABLES, not from input
                for path, strategy in files:
                    if not path.exists():
                        continue
                    lines, bad = self._load(conn, path, strategy)
                    corrupt += bad
                    self._touch(conn, path, strategy, lines=lines)
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('corrupt', ?)", (str(corrupt),))
        return self.counts()

    def _load(self, conn: sqlite3.Connection, path: Path, strategy: str | None) -> tuple[int, int]:
        """Insert every record of `path`; return (lines read, lines skipped). A record the index cannot read leaves
        nothing behind: its rows are rolled back to the savepoint taken before it.
        """
        lines = corrupt = 0
        with path.open("rb") as fh:
            for raw in fh:
                lines += 1
                try:
                    record = json.loads(raw.decode("utf-8"))
                except ValueError:  # not UTF-8 (a write cut inside a character) or not JSON; a blank line is neither
                    corrupt += bool(raw.strip())
                    continue
                if not isinstance(record, dict):
                    corrupt += 1
                    continue
                conn.execute("SAVEPOINT record")
                try:
                    self._insert(conn, record, path.name, strategy)
                except UNREADABLE:
                    conn.execute("ROLLBACK TO record")
                    corrupt += 1
                conn.execute("RELEASE record")
        return lines, corrupt

    @property
    def corrupt(self) -> int:
        """Return how many lines of the ledger files the last rebuild could not read."""
        if not self.path.exists():
            return 0
        try:
            with self._open() as conn:
                row = conn.execute("SELECT value FROM meta WHERE key = 'corrupt'").fetchone()
        except sqlite3.Error:
            return 0
        return int(row[0]) if row else 0

    def stale(self, ledgers: LedgerFiles) -> bool:
        """Return whether the index no longer matches `ledgers`: a file whose size or modification time it does not
        know (something wrote to it directly, or rewrote it), a file it knows that is gone, or no usable index.
        """
        if not self.path.exists():
            return True
        try:
            with self._open() as conn:
                known = {name: (int(b), int(m)) for name, b, m in conn.execute("SELECT name, bytes, mtime_ns FROM source")}
        except sqlite3.Error:
            return True
        present = {path.name: _signature(path) for path, _ in ledgers if path.exists()}
        return any(known.get(name) != sig for name, sig in present.items()) or any(n not in present for n in known)

    def sync(self, ledgers: LedgerFiles) -> bool:
        """Rebuild the index when `stale` says so; return whether it was rebuilt."""
        files = list(ledgers)
        if not self.stale(files):
            return False
        self.rebuild(files)
        return True

    def task_records(self, *, source: str | None = None, tagged: bool = False) -> list[dict[str, Any]]:
        """Return the task records (of one `source` file, or all), overrides of the same file applied as
        `Ledger.tasks` does; `tagged` adds each record's `source` (the file name) and `strategy`.
        """
        where, params = (" WHERE source = ?", (source,)) if source is not None else ("", ())
        with self._open() as conn:
            rows = conn.execute("SELECT source, task_id, strategy, record FROM task" + where + " ORDER BY rowid", params)
            tasks = {(src, task_id): json.loads(record) | ({"source": src, "strategy": strategy} if tagged else {})
                     for src, task_id, strategy, record in rows}
            overrides = conn.execute("SELECT source, task_id, passed FROM override" + where + " ORDER BY rowid",
                                     params).fetchall()
        for src, task_id, passed in overrides:
            t = tasks.get((src, task_id))
            if t is not None:
                t["status"] = "solved" if passed else "unsolved"
                t["overridden"] = True
        return list(tasks.values())

    def counts(self) -> dict[str, int]:
        """Return how many rows each table holds: tasks, attempts, verdicts, overrides, sources."""
        with self._open() as conn:
            return {name + "s": int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])  # noqa: S608
                    for name in TABLES}
