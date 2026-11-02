"""Snowflake via snowflake-connector-python.

Connection params (gulliver.toml):
  account, user, warehouse, database, schema, role   (all optional except account/user)
  authenticator               e.g. "externalbrowser" for SSO
  credits_per_hour            warehouse size in credits/hour (XS=1, S=2, M=4, L=8, ...; default 1)
  credit_usd                  price of one credit (default 3.00)

Password: environment variable SNOWFLAKE_PASSWORD (or the name in `password_env`).
Private key auth: set `private_key_file` and SNOWFLAKE_PRIVATE_KEY_PASSPHRASE.

Snowflake bills warehouse time, not bytes. EXPLAIN gives the bytes and partitions that
may be read (an upper bound after pruning); money is predicted from rehearsal runtimes.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import sqlglot
from sqlglot import exp

from ..config import Connection
from .base import UPPER_BOUND, Engine, EngineError, Estimate, ExecResult, MissingDriver, TableInfo


class SnowflakeEngine(Engine):
    name = "snowflake"
    dialect = "snowflake"
    billed = True

    def __init__(self, connection: Connection, read_only: bool = True, _conn: Any = None):
        super().__init__(connection, read_only)
        self.credits_per_hour = float(self.params.get("credits_per_hour", 1))
        self.credit_usd = float(self.params.get("credit_usd", 3.0))
        if _conn is not None:
            self.conn = _conn
            return
        try:
            import snowflake.connector
        except ImportError as exc:
            raise MissingDriver("Snowflake needs snowflake-connector-python: pip install 'gulliver-mcp[snowflake]'") from exc
        kwargs: dict[str, Any] = {
            k: self.params[k]
            for k in ("account", "user", "warehouse", "database", "schema", "role", "authenticator",
                      "private_key_file")
            if self.params.get(k)
        }
        password = os.environ.get(str(self.params.get("password_env", "SNOWFLAKE_PASSWORD")))
        if password:
            kwargs["password"] = password
        if self.params.get("private_key_file") and os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"):
            kwargs["private_key_file_pwd"] = os.environ["SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"]
        kwargs.setdefault("session_parameters", {})["QUERY_TAG"] = "gulliver"
        try:
            self.conn = snowflake.connector.connect(**kwargs)
        except Exception as exc:
            raise EngineError(f"Could not connect to Snowflake: {str(exc).splitlines()[0]}") from exc

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover
            pass

    def _run(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        try:
            cur = self.conn.cursor()
            try:
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else []
            finally:
                cur.close()
        except Exception as exc:
            raise EngineError(str(exc).splitlines()[0]) from exc

    # -- metadata ------------------------------------------------------------------
    _TABLES_SQL = ("SELECT table_catalog, table_schema, table_name, row_count, bytes, clustering_key, table_type "
                   "FROM information_schema.tables WHERE table_schema <> 'INFORMATION_SCHEMA'")

    @staticmethod
    def _to_info(row: tuple) -> TableInfo:
        catalog, schema, name, rows, size, clustering, table_type = row
        return TableInfo(name=f"{schema}.{name}", rows=int(rows) if rows is not None else None,
                         bytes=int(size) if size is not None else None,
                         clustered_by=clustering or None, kind=str(table_type or "TABLE").lower())

    def list_tables(self, limit: int = 50) -> list[TableInfo]:
        rows = self._run(self._TABLES_SQL + " ORDER BY bytes DESC NULLS LAST LIMIT %s", [int(limit)])
        return [self._to_info(r) for r in rows]

    def table_info(self, ref: str) -> TableInfo | None:
        try:
            node = sqlglot.parse_one(f"SELECT * FROM {ref}", read="snowflake").find(exp.Table)
        except sqlglot.errors.ParseError:
            return None
        if node is None or not isinstance(node.this, exp.Identifier):
            return None
        query = self._TABLES_SQL + " AND table_name = %s"
        params: list[Any] = [node.name.upper()]
        if node.db:
            query += " AND table_schema = %s"
            params.append(node.db.upper())
        rows = self._run(query + " LIMIT 1", params)
        if not rows:
            return None
        info = self._to_info(rows[0])
        info.name = ref
        return info

    # -- estimation ----------------------------------------------------------------
    def estimate(self, sql: str) -> Estimate:
        rows = self._run(f"EXPLAIN USING JSON {sql}")
        doc = rows[0][0] if rows else "{}"
        return estimate_from_explain(json.loads(doc) if isinstance(doc, str) else doc)

    def cost_for(self, bytes_processed: int | None, runtime_s: float | None) -> float | None:
        if runtime_s is None:
            return None
        return runtime_s / 3600.0 * self.credits_per_hour * self.credit_usd

    # -- execution -----------------------------------------------------------------
    def execute(self, sql: str, preview_rows: int, timeout_s: float | None = None,
                max_bytes_billed: int | None = None) -> ExecResult:
        if timeout_s:
            self._run(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {max(1, int(timeout_s))}")
        if self.tags:  # QUERY_TAG shows up in QUERY_HISTORY, so spend can be split per agent and receipt
            self._run("ALTER SESSION SET QUERY_TAG = %s", [json.dumps(self.tags, sort_keys=True)[:2000]])
        start = time.perf_counter()
        preview: list[dict[str, Any]] = []
        total = 0
        columns: list[str] = []
        query_id = None
        try:
            cur = self.conn.cursor()
            try:
                cur.execute(sql)
                query_id = getattr(cur, "sfqid", None)
                if cur.description:
                    columns = [d[0] for d in cur.description]
                    while True:
                        batch = cur.fetchmany(10_000)
                        if not batch:
                            break
                        for row in batch[: max(0, preview_rows - len(preview))]:
                            preview.append({c: _jsonable(v) for c, v in zip(columns, row, strict=False)})
                        total += len(batch)
            finally:
                cur.close()
        except Exception as exc:
            raise EngineError(str(exc).splitlines()[0]) from exc
        elapsed = time.perf_counter() - start
        scanned = None
        if query_id:
            try:
                hist = self._run(
                    "SELECT bytes_scanned FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION()) "
                    "WHERE query_id = %s", [query_id])
                scanned = int(hist[0][0]) if hist and hist[0][0] is not None else None
            except EngineError:
                scanned = None
        return ExecResult(columns=columns, preview=preview, rows=total, elapsed_s=elapsed, bytes_processed=scanned,
                          cost_usd=self.cost_for(None, elapsed), truncated=total > len(preview),
                          notes=["cost assumes the warehouse runs only this query; resumes bill at least 60s"])

    def data_version(self, tables: list[str]) -> str | None:
        """LAST_ALTERED (moves on every DML and DDL) + rows + bytes of each base table; None for views,
        external and dynamic tables."""
        if not tables:
            return None
        parts = []
        for ref in tables:
            try:
                node = sqlglot.parse_one(f"SELECT * FROM {ref}", read="snowflake").find(exp.Table)
            except sqlglot.errors.ParseError:
                return None
            if node is None or not isinstance(node.this, exp.Identifier) or node.catalog:
                return None  # another database's information_schema: not checked here
            query = ("SELECT table_type, last_altered, row_count, bytes FROM information_schema.tables "
                     "WHERE table_name = %s")
            params: list[Any] = [node.name.upper()]
            if node.db:
                query += " AND table_schema = %s"
                params.append(node.db.upper())
            try:
                rows = self._run(query, params)
            except EngineError:
                return None
            if len(rows) != 1 or str(rows[0][0]).upper() != "BASE TABLE" or rows[0][1] is None:
                return None
            parts.append(f"{ref}:{rows[0][1]}:{rows[0][2]}:{rows[0][3]}")
        return "snowflake:" + "|".join(parts)

    def md5_int(self, text_expr: str) -> str:
        return f"TO_NUMBER(SUBSTR(MD5({text_expr}), 1, 7), 'XXXXXXX')"

    def sample_clause(self, fraction: float, seed: int, method: str) -> str:
        from ..sampling import percent_text

        kind = "BERNOULLI" if method == "bernoulli" else "SYSTEM"
        return f"SAMPLE {kind} ({percent_text(fraction)}) SEED ({int(seed)})"


def estimate_from_explain(doc: dict[str, Any]) -> Estimate:
    stats = (doc or {}).get("GlobalStats", {}) or {}
    total = stats.get("partitionsTotal")
    assigned = stats.get("partitionsAssigned")
    notes = ["Snowflake bills warehouse time: rehearse to predict runtime and money"]
    if total:
        notes.append(f"partitions to read: {assigned} of {total} (pruning {100 - 100 * (assigned or 0) / total:.0f}%)")
    return Estimate(bytes=stats.get("bytesAssigned"), rows=None, cost_usd=None, accuracy=UPPER_BOUND,
                    source="snowflake EXPLAIN USING JSON", notes=notes)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
