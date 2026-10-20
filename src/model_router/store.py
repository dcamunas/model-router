"""SQLite index of the ledger: the same records as the JSON-lines files, in tables that can be queried
across months (`router stats`, the calibration of the start table). The JSON-lines files stay the
source: they only append, they survive a cut, and they rebuild this file at any time (`router db
rebuild`). Every task row also keeps its original record, so the columns lose nothing, and the only
trace of a prompt is its fingerprint: the database stores no prompt or answer, as the ledger does not.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .catalog import UnknownModelError, parse_cell

SCHEMA_VERSION = 1
_LOCK = threading.Lock()
LedgerFiles = Iterable[tuple[Path, str | None]]  # (file, strategy name when it is a bench ledger)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source (name TEXT PRIMARY KEY, strategy TEXT, bytes INTEGER NOT NULL, lines INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS task (
    task_id TEXT PRIMARY KEY, source TEXT NOT NULL, strategy TEXT, fingerprint TEXT, kind TEXT NOT NULL,
    risk TEXT NOT NULL, status TEXT NOT NULL, verify TEXT, started_at TEXT, elapsed REAL, final_cell TEXT,
    retries INTEGER NOT NULL DEFAULT 0, work_cost_usd REAL NOT NULL DEFAULT 0, verify_cost_usd REAL NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0, router_version TEXT, calibration_id TEXT, eval_id TEXT, repeat INTEGER,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_kind ON task(kind, risk);
CREATE INDEX IF NOT EXISTS task_eval ON task(eval_id);
CREATE TABLE IF NOT EXISTS attempt (
    task_id TEXT NOT NULL, n INTEGER NOT NULL, cell TEXT NOT NULL, served_model TEXT, stop_reason TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
    verdict INTEGER, verdict_source TEXT, reason TEXT, verifier TEXT, verify_cost_usd REAL NOT NULL DEFAULT 0,
    arbiter TEXT, carried_reason INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (task_id, n)
);
CREATE INDEX IF NOT EXISTS attempt_cell ON attempt(cell);
CREATE TABLE IF NOT EXISTS verdict (
    task_id TEXT NOT NULL, n INTEGER NOT NULL, idx INTEGER NOT NULL, cell TEXT NOT NULL, provider TEXT,
    role TEXT NOT NULL, passed INTEGER NOT NULL, confidence REAL, reason TEXT, PRIMARY KEY (task_id, n, idx)
);
CREATE TABLE IF NOT EXISTS override (task_id TEXT NOT NULL, at TEXT NOT NULL, passed INTEGER NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS calibration (
    id TEXT PRIMARY KEY, created TEXT NOT NULL, eval_id TEXT, tables TEXT NOT NULL, metrics TEXT,
    active INTEGER NOT NULL DEFAULT 0
);
"""
TABLES = ("task", "attempt", "verdict", "override", "source")


def _provider(label: str) -> str | None:
    try:
        return parse_cell(label).spec.provider
    except (UnknownModelError, ValueError):
        return None


class Store:
    """The SQLite index at `path`. `record` indexes one ledger record as it is written; `rebuild` reloads
    everything from the ledger files; `task_records` gives the records back in the shape `Ledger.tasks`
    returns, overrides applied, so `summarize` works on either.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        return conn

    def record(self, record: dict[str, Any], *, source: Path, strategy: str | None = None) -> None:
        """Index `record` (a task, an override or a bench note) that `source` just appended as one JSON line, and
        remember the file's size so `stale` can tell when something wrote to it without passing through here.
        """
        appended = len((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))  # what Ledger wrote
        with _LOCK, self._connect() as conn:
            self._insert(conn, record, source.name, strategy)
            self._touch(conn, source, strategy, appended=appended)

    def _touch(
        self, conn: sqlite3.Connection, source: Path, strategy: str | None, *, appended: int = 0, lines: int | None = None
    ) -> None:
        """Remember `source`'s size: the real one after a rebuild (`lines` given), or after a live write only when
        the file grew by exactly the record just appended; otherwise -1, unknown, so the next sync rebuilds.
        """
        size = source.stat().st_size if source.exists() else 0
        row = conn.execute("SELECT strategy, bytes, lines FROM source WHERE name = ?", (source.name,)).fetchone()
        strategy = strategy or (row[0] if row else None)
        if lines is None:
            before = int(row[1]) if row else 0
            known = size if (row is None or before >= 0) and before + appended == size else -1
            lines = (int(row[2]) if row else 0) + 1
        else:
            known = size
        conn.execute("INSERT OR REPLACE INTO source (name, strategy, bytes, lines) VALUES (?, ?, ?, ?)",
                     (source.name, strategy, known, lines))

    def _insert(self, conn: sqlite3.Connection, record: dict[str, Any], source: str, strategy: str | None) -> None:
        kind = record.get("type")
        if kind == "override":
            conn.execute("INSERT INTO override (task_id, at, passed, note) VALUES (?, ?, ?, ?)",
                         (record["task_id"], record.get("at", ""), int(bool(record["passed"])), record.get("note", "")))
        elif kind == "bench":
            conn.execute("INSERT OR REPLACE INTO source (name, strategy, bytes, lines) VALUES (?, ?, 0, 0)",
                         (source, record.get("strategy"), ))
        elif kind == "task":
            self._insert_task(conn, record, source, strategy)

    def _insert_task(self, conn: sqlite3.Connection, record: dict[str, Any], source: str, strategy: str | None) -> None:
        task_id = record["task_id"]
        if strategy is None:
            row = conn.execute("SELECT strategy FROM source WHERE name = ?", (source,)).fetchone()
            strategy = row[0] if row else None
        conn.execute("DELETE FROM attempt WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM verdict WHERE task_id = ?", (task_id,))
        conn.execute(
            "INSERT OR REPLACE INTO task (task_id, source, strategy, fingerprint, kind, risk, status, verify, started_at, "
            "elapsed, final_cell, retries, work_cost_usd, verify_cost_usd, total_cost_usd, router_version, calibration_id, "
            "eval_id, repeat, record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, source, strategy, record.get("fingerprint"), record.get("kind", "unknown"), record.get("risk", "medium"),
             record.get("status", "unsolved"), record.get("verify"), record.get("started_at"), record.get("elapsed"),
             record.get("final_cell"), int(record.get("retries", 0)), float(record.get("work_cost_usd", 0.0)),
             float(record.get("verify_cost_usd", 0.0)), float(record.get("total_cost_usd", 0.0)),
             record.get("router_version"), record.get("calibration_id"), record.get("eval_id"), record.get("repeat"),
             json.dumps(record, ensure_ascii=False)),
        )
        for a in record.get("attempts") or []:
            usage = a.get("usage") or {}
            verdict = a.get("verdict")
            conn.execute(
                "INSERT OR REPLACE INTO attempt (task_id, n, cell, served_model, stop_reason, input_tokens, output_tokens, "
                "cost_usd, verdict, verdict_source, reason, verifier, verify_cost_usd, arbiter, carried_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, int(a.get("n", 0)), a.get("cell", "?"), a.get("served_model"), a.get("stop_reason"),
                 int(usage.get("input", 0)), int(usage.get("output", 0)), float(a.get("cost_usd", 0.0)),
                 None if verdict is None else int(bool(verdict)), a.get("verdict_source"), a.get("reason"),
                 a.get("verifier"), float(a.get("verify_cost_usd", 0.0)), a.get("arbiter"),
                 int(bool(a.get("carried_reason", False)))),
            )
            for idx, v in enumerate(a.get("verdicts") or []):
                conn.execute(
                    "INSERT OR REPLACE INTO verdict (task_id, n, idx, cell, provider, role, passed, confidence, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, int(a.get("n", 0)), idx, v.get("cell", "?"), _provider(str(v.get("cell", ""))),
                     v.get("role", "judge"), int(bool(v.get("passed"))), v.get("confidence"), v.get("reason")),
                )

    def rebuild(self, ledgers: LedgerFiles) -> dict[str, int]:
        """Drop every row and reload the index from `ledgers`; lines that are not JSON are skipped and counted in
        `corrupt`. Return `counts`.
        """
        corrupt = 0
        with _LOCK, self._connect() as conn:
            for name in TABLES:
                conn.execute(f"DELETE FROM {name}")  # noqa: S608 - table names come from TABLES, not from input
            for path, strategy in ledgers:
                if not path.exists():
                    continue
                lines = 0
                with path.open(encoding="utf-8") as fh:
                    for line in fh:
                        lines += 1
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                        except ValueError:
                            corrupt += 1
                            continue
                        if isinstance(record, dict):
                            self._insert(conn, record, path.name, strategy)
                self._touch(conn, path, strategy, lines=lines)
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('corrupt', ?)", (str(corrupt),))
        return self.counts()

    @property
    def corrupt(self) -> int:
        """Return how many lines of the ledger files the last rebuild could not read."""
        if not self.path.exists():
            return 0
        with _LOCK, self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'corrupt'").fetchone()
        return int(row[0]) if row else 0

    def stale(self, ledgers: LedgerFiles) -> bool:
        """Return whether any of `ledgers` has a size the index does not know: something wrote to it directly."""
        if not self.path.exists():
            return True
        with _LOCK, self._connect() as conn:
            known = {name: int(size) for name, size in conn.execute("SELECT name, bytes FROM source")}
        return any(path.exists() and known.get(path.name, -1) != path.stat().st_size for path, _ in ledgers)

    def sync(self, ledgers: LedgerFiles) -> bool:
        """Rebuild the index when `stale` says so; return whether it was rebuilt."""
        files = list(ledgers)
        if not self.stale(files):
            return False
        self.rebuild(files)
        return True

    def task_records(self, *, source: str | None = None) -> list[dict[str, Any]]:
        """Return the task records (of one `source` file, or all), overrides applied as `Ledger.tasks` does."""
        with _LOCK, self._connect() as conn:
            query, params = "SELECT task_id, record FROM task", ()
            if source is not None:
                query, params = query + " WHERE source = ?", (source,)
            tasks = {task_id: json.loads(record) for task_id, record in conn.execute(query + " ORDER BY rowid", params)}
            overrides = conn.execute("SELECT task_id, passed FROM override ORDER BY rowid").fetchall()
        for task_id, passed in overrides:
            if task_id in tasks:
                tasks[task_id]["status"] = "solved" if passed else "unsolved"
                tasks[task_id]["overridden"] = True
        return list(tasks.values())

    def counts(self) -> dict[str, int]:
        """Return how many rows each table holds: tasks, attempts, verdicts, overrides, sources."""
        with _LOCK, self._connect() as conn:
            return {name + "s": int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])  # noqa: S608
                    for name in TABLES}
