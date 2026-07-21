"""Warehouse layer: the queryable record of everything the platform did.

Local runs use SQLite. Production uses BigQuery. Both satisfy the same interface,
so agents and the observability layer never know which one is behind them -
that is what makes "swap SQLite for BigQuery" a config change.

Four tables, and the reason each exists:
  events         - every trigger received, for idempotency and replay
  run_steps      - every step of every agent run, for the "why" trace
  golden_records - the account record this platform has write authority over
  dead_letters   - anything we could not process, so nothing is ever silently lost
"""

from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config, data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    source       TEXT NOT NULL,
    account_id   TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    payload      TEXT NOT NULL,
    trace_ids    TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS run_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id    TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    agent       TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    step        TEXT NOT NULL,
    status      TEXT NOT NULL,
    latency_ms  INTEGER,
    detail      TEXT NOT NULL DEFAULT '{}',
    error       TEXT,
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_steps_trace ON run_steps(trace_id, seq);
CREATE INDEX IF NOT EXISTS idx_run_steps_agent ON run_steps(agent, ts);

CREATE TABLE IF NOT EXISTS golden_records (
    account_id      TEXT PRIMARY KEY,
    data            TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    updated_by      TEXT NOT NULL,
    trace_id        TEXT NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS dead_letters (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT,
    reason     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    ts         TEXT NOT NULL
);

-- Closes the loop. A human marks each alert correct/wrong from the dashboard and
-- the agent reads the resulting precision back at analysis time.
CREATE TABLE IF NOT EXISTS outcomes (
    trace_id    TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    driver      TEXT NOT NULL,
    severity    TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    notes       TEXT,
    reviewer    TEXT,
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_driver ON outcomes(agent, driver);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConcurrentUpdate(RuntimeError):
    """Another writer modified the golden record first.

    Raised rather than swallowed: on the record this platform claims write
    authority over, a lost update is a data-quality bug, not an inconvenience.
    """


def merge_golden_record(
    warehouse: "Warehouse", account_id: str, fields: dict[str, Any],
    updated_by: str, trace_id: str, attempts: int = 3,
) -> dict[str, Any]:
    """Read-modify-write the golden record safely under concurrency.

    Two agents reacting to the same account at once is normal on a shared
    platform, so the write re-reads and retries on conflict instead of
    clobbering. Retrying is correct here because the operation is a field merge:
    replaying it against the newer revision yields the same intent.
    """
    last_error: Exception | None = None

    for _ in range(attempts):
        current = warehouse.get_golden_record(account_id)
        expected = current["revision"] if current else 0
        try:
            return warehouse.upsert_golden_record(
                account_id, fields, updated_by, trace_id, expected_revision=expected
            )
        except ConcurrentUpdate as exc:
            last_error = exc
            continue

    raise ConcurrentUpdate(
        f"could not write golden record {account_id} after {attempts} attempts: {last_error}"
    )


class Warehouse(ABC):
    """The storage contract. Swapping the implementation must not change callers."""

    @abstractmethod
    def record_event(self, event: Any) -> bool:
        """Insert an event. Returns False if already seen (idempotent replay)."""

    @abstractmethod
    def attach_trace(self, event_id: str, trace_id: str) -> None: ...

    @abstractmethod
    def record_step(self, row: dict[str, Any]) -> None: ...

    @abstractmethod
    def steps_for_trace(self, trace_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def recent_traces(self, limit: int = 50) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_event(self, event_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def upsert_golden_record(
        self, account_id: str, data: dict[str, Any], updated_by: str, trace_id: str
    ) -> dict[str, Any]: ...

    @abstractmethod
    def get_golden_record(self, account_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def dead_letter(self, source: str | None, reason: str, payload: Any) -> None: ...

    @abstractmethod
    def dead_letters(self, limit: int = 50) -> list[dict[str, Any]]: ...

    @abstractmethod
    def record_outcome(self, row: dict[str, Any]) -> None: ...

    @abstractmethod
    def outcomes(self, agent: str | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def traces_for_account(self, account_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def steps_named(self, step: str, limit: int = 100) -> list[dict[str, Any]]: ...

    @abstractmethod
    def llm_spend_since(self, since_iso: str) -> float: ...

    @abstractmethod
    def count_llm_calls_for_account(self, account_id: str, since_iso: str) -> int: ...

    @abstractmethod
    def spend_by(self, field: str, since_iso: str) -> dict[str, float]: ...


class SQLiteWarehouse(Warehouse):
    """Local implementation. Same interface as BigQuery, no external dependency."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        # FastAPI serves requests on a threadpool; one connection guarded by a lock
        # is simpler and safer here than per-thread connections.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def record_event(self, event: Any) -> bool:
        with self._lock:
            existing = self._conn.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (event.event_id,)
            ).fetchone()
            if existing:
                return False

            self._conn.execute(
                """INSERT INTO events
                   (event_id, event_type, source, account_id, occurred_at, received_at, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.event_type,
                    event.source,
                    event.account_id,
                    event.occurred_at.isoformat(),
                    _now(),
                    json.dumps(event.payload, default=str),
                ),
            )
            self._conn.commit()
            return True

    def attach_trace(self, event_id: str, trace_id: str) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT trace_ids FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return
            traces = json.loads(row["trace_ids"])
            if trace_id not in traces:
                traces.append(trace_id)
            self._conn.execute(
                "UPDATE events SET trace_ids = ? WHERE event_id = ?",
                (json.dumps(traces), event_id),
            )
            self._conn.commit()

    def record_step(self, row: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO run_steps
                   (trace_id, event_id, agent, seq, step, status, latency_ms, detail, error, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["trace_id"],
                    row["event_id"],
                    row["agent"],
                    row["seq"],
                    row["step"],
                    row["status"],
                    row.get("latency_ms"),
                    json.dumps(row.get("detail", {}), default=str),
                    row.get("error"),
                    row.get("ts") or _now(),
                ),
            )
            self._conn.commit()

    def steps_for_trace(self, trace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM run_steps WHERE trace_id = ? ORDER BY seq ASC", (trace_id,)
            ).fetchall()
        return [self._step_row(r) for r in rows]

    def recent_traces(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT trace_id, agent, event_id,
                          MIN(ts)  AS started_at,
                          MAX(ts)  AS finished_at,
                          COUNT(*) AS steps,
                          SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
                          SUM(COALESCE(latency_ms, 0)) AS total_ms
                   FROM run_steps
                   GROUP BY trace_id
                   ORDER BY started_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["payload"] = json.loads(record["payload"])
        record["trace_ids"] = json.loads(record["trace_ids"])
        return record

    def upsert_golden_record(
        self, account_id: str, data: dict[str, Any], updated_by: str, trace_id: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Merge fields into the golden record.

        Pass `expected_revision` to make the write optimistic: if another writer
        has bumped the revision since you read it, this raises rather than
        silently discarding their change. Without it the call is last-write-wins,
        which is only safe for a single writer.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT data, revision FROM golden_records WHERE account_id = ?",
                (account_id,),
            ).fetchone()

            current_revision = row["revision"] if row else 0
            if expected_revision is not None and current_revision != expected_revision:
                raise ConcurrentUpdate(
                    f"golden record {account_id} is at revision {current_revision}, "
                    f"expected {expected_revision}: another writer got there first"
                )

            if row is None:
                merged = data
            else:
                # Merge rather than overwrite: this platform has write authority over
                # its own fields, not over columns other systems own.
                merged = {**json.loads(row["data"]), **data}
            revision = current_revision + 1

            # The revision guard is repeated in SQL so the check and the write are
            # one atomic statement, not a read followed by a hopeful write.
            if row is None:
                self._conn.execute(
                    """INSERT INTO golden_records
                           (account_id, data, updated_at, updated_by, trace_id, revision)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (account_id, json.dumps(merged, default=str), _now(),
                     updated_by, trace_id, revision),
                )
            else:
                cursor = self._conn.execute(
                    """UPDATE golden_records
                       SET data = ?, updated_at = ?, updated_by = ?, trace_id = ?, revision = ?
                       WHERE account_id = ? AND revision = ?""",
                    (json.dumps(merged, default=str), _now(), updated_by, trace_id,
                     revision, account_id, current_revision),
                )
                if cursor.rowcount == 0:
                    raise ConcurrentUpdate(
                        f"golden record {account_id} changed during the write"
                    )
            self._conn.commit()

        return {"account_id": account_id, "revision": revision, "data": merged}

    def get_golden_record(self, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM golden_records WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["data"] = json.loads(record["data"])
        return record

    def dead_letter(self, source: str | None, reason: str, payload: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO dead_letters (source, reason, payload, ts) VALUES (?, ?, ?, ?)",
                (source, reason, json.dumps(payload, default=str), _now()),
            )
            self._conn.commit()

    def dead_letters(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM dead_letters ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["payload"] = json.loads(record["payload"])
            out.append(record)
        return out

    def llm_spend_since(self, since_iso: str) -> float:
        """Total LLM spend recorded since a timestamp.

        Reads the same trace rows the dashboard shows, so the budget is enforced
        against the number we report rather than a second counter that can drift.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT detail FROM run_steps WHERE step = 'llm_cost' AND ts >= ?",
                (since_iso,),
            ).fetchall()
        return sum(float(json.loads(r["detail"]).get("cost_usd") or 0) for r in rows)

    def count_llm_calls_for_account(self, account_id: str, since_iso: str) -> int:
        """LLM calls made about one account since a timestamp.

        Backs the per-account throttle: a health score that flaps must not be
        able to spend the whole daily budget on a single customer.
        """
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n
                   FROM run_steps rs JOIN events e ON rs.event_id = e.event_id
                   WHERE rs.step = 'llm_cost' AND e.account_id = ? AND rs.ts >= ?""",
                (account_id, since_iso),
            ).fetchone()
        return int(row["n"]) if row else 0

    def spend_by(self, field: str, since_iso: str) -> dict[str, float]:
        """LLM spend grouped by 'agent' or by analysis driver, for cost reporting."""
        column = "rs.agent" if field == "agent" else "rs.agent"
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT {column} AS k, detail FROM run_steps rs
                    WHERE rs.step = 'llm_cost' AND rs.ts >= ?""",
                (since_iso,),
            ).fetchall()
        totals: dict[str, float] = {}
        for row in rows:
            cost = float(json.loads(row["detail"]).get("cost_usd") or 0)
            totals[row["k"]] = round(totals.get(row["k"], 0.0) + cost, 6)
        return totals

    def steps_named(self, step: str, limit: int = 100) -> list[dict[str, Any]]:
        """All occurrences of one step across every run - for guardrail reporting."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM run_steps WHERE step = ? ORDER BY id DESC LIMIT ?""",
                (step, limit),
            ).fetchall()
        return [self._step_row(r) for r in rows]

    def traces_for_account(self, account_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT rs.trace_id, rs.agent, e.event_type, e.source,
                          MIN(rs.ts) AS started_at
                   FROM run_steps rs
                   JOIN events e ON rs.event_id = e.event_id
                   WHERE e.account_id = ?
                   GROUP BY rs.trace_id
                   ORDER BY started_at DESC""",
                (account_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_outcome(self, row: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO outcomes
                       (trace_id, agent, account_id, driver, severity, verdict, notes, reviewer, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(trace_id) DO UPDATE SET
                       verdict = excluded.verdict,
                       notes = excluded.notes,
                       reviewer = excluded.reviewer,
                       ts = excluded.ts""",
                (
                    row["trace_id"], row["agent"], row["account_id"], row["driver"],
                    row["severity"], row["verdict"], row.get("notes"),
                    row.get("reviewer"), _now(),
                ),
            )
            self._conn.commit()

    def outcomes(self, agent: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM outcomes"
        params: tuple[Any, ...] = ()
        if agent:
            query += " WHERE agent = ?"
            params = (agent,)
        query += " ORDER BY ts DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _step_row(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["detail"] = json.loads(record["detail"])
        return record


def build_warehouse(config: Config) -> Warehouse:
    """Factory. The only place that decides which warehouse implementation runs."""
    kind = config.get("platform.warehouse", "sqlite")

    if kind == "sqlite":
        configured = config.get("platform.sqlite_path", "./data/platform.db")
        path = Path(configured)
        if not path.is_absolute():
            path = data_dir() / path.name
        return SQLiteWarehouse(path)

    if kind == "bigquery":
        # Imported lazily so the BigQuery client stays an optional dependency:
        # nobody running this locally should need google-cloud-bigquery installed.
        from .warehouse_bigquery import BigQueryWarehouse

        return BigQueryWarehouse(
            project=config.require("platform.bigquery.project"),
            dataset=config.require("platform.bigquery.dataset"),
        )

    raise ValueError(f"Unknown warehouse type: {kind}")
