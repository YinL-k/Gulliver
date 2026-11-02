"""PostgreSQL via psycopg 3. Self-hosted: no per-query price, so the budget is bytes and time.

Connection params: dsn (postgresql://user@host:5432/db). Put the password in PGPASSWORD
or ~/.pgpass, never in gulliver.toml.
"""

from __future__ import annotations

import json
import time
from typing import Any

import sqlglot
from sqlglot import exp

from ..config import Connection
from .base import HEURISTIC, Engine, EngineError, Estimate, ExecResult, MissingDriver, TableInfo


class PostgresEngine(Engine):
    name = "postgres"
    dialect = "postgres"
    billed = False

    def __init__(self, connection: Connection, read_only: bool = True, _conn: Any = None):
        super().__init__(connection, read_only)
        if _conn is not None:  # injected (tests)
            self.conn = _conn
            return
        try:
            import psycopg
        except ImportError as exc:
            raise MissingDriver("PostgreSQL needs psycopg: pip install 'gulliver-mcp[postgres]'") from exc
        dsn = self.params.get("dsn")
        if not dsn:
            raise EngineError("Postgres connection needs a 'dsn' (postgresql://user@host:5432/db).")
        try:
            self.conn = psycopg.connect(dsn, autocommit=True)
        except Exception as exc:
            raise EngineError(f"Could not connect to Postgres: {str(exc).splitlines()[0]}") from exc
        if read_only:
            self._run("SET default_transaction_read_only = on")

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover
            pass

    def _run(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else []
        except Exception as exc:
            raise EngineError(str(exc).splitlines()[0]) from exc

    # -- metadata ------------------------------------------------------------------
    _TABLES_SQL = """
        SELECT n.nspname, c.relname, GREATEST(c.reltuples, 0)::bigint,
               CASE WHEN c.relkind = 'p'
                    THEN (SELECT COALESCE(SUM(pg_total_relation_size(p.relid)), 0) FROM pg_partition_tree(c.oid) p)
                    ELSE pg_total_relation_size(c.oid) END,
               CASE WHEN c.relkind = 'p' THEN pg_get_partkeydef(c.oid) END, c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'm') AND NOT c.relispartition
          AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%%'
    """

    def _to_info(self, row: tuple) -> TableInfo:
        schema, name, rows, size, partkey, relkind = row
        part = None
        if partkey:
            # "RANGE (created_at)" -> "created_at"
            part = partkey.split("(", 1)[-1].rstrip(")").strip()
        full = name if schema == "public" else f"{schema}.{name}"
        info = TableInfo(name=full, rows=int(rows) if rows is not None else None,
                         bytes=int(size) if size is not None else None, partitioned_by=part,
                         kind="materialized view" if relkind == "m" else "table")
        info.notes.append("rows come from planner statistics (run ANALYZE if they look off)")
        return info

    def list_tables(self, limit: int = 50) -> list[TableInfo]:
        rows = self._run(self._TABLES_SQL + " ORDER BY 4 DESC LIMIT %s", [int(limit)])
        return [self._to_info(r) for r in rows]

    def table_info(self, ref: str) -> TableInfo | None:
        try:
            node = sqlglot.parse_one(f"SELECT * FROM {ref}", read="postgres").find(exp.Table)
        except sqlglot.errors.ParseError:
            return None
        if node is None or not isinstance(node.this, exp.Identifier):
            return None
        rows = self._run(self._TABLES_SQL + " AND c.relname = %s AND n.nspname = %s LIMIT 1",
                         [node.name, node.db or "public"])
        if not rows:
            return None
        info = self._to_info(rows[0])
        cols = self._run(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            [node.db or "public", node.name],
        )
        info.columns = [{"name": c, "type": t} for c, t in cols]
        info.name = ref
        return info

    # -- estimation ----------------------------------------------------------------
    def estimate(self, sql: str) -> Estimate:
        rows = self._run(f"EXPLAIN (FORMAT JSON) {sql}")
        plan_doc = rows[0][0] if rows else []
        if isinstance(plan_doc, str):
            plan_doc = json.loads(plan_doc)
        return estimate_from_plan(plan_doc, self._relation_bytes)

    def _relation_bytes(self, schema: str | None, relation: str) -> int | None:
        rows = self._run(
            "SELECT pg_relation_size(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = %s AND n.nspname = %s",
            [relation, schema or "public"],
        )
        return int(rows[0][0]) if rows else None

    # -- execution -----------------------------------------------------------------
    def execute(self, sql: str, preview_rows: int, timeout_s: float | None = None,
                max_bytes_billed: int | None = None) -> ExecResult:
        if timeout_s:
            self._run(f"SET statement_timeout = {int(timeout_s * 1000)}")
        if self.tags:  # visible in pg_stat_activity and the server log while it runs
            self._run("SELECT set_config('application_name', %s, false)", [application_name(self.tags)])
        start = time.perf_counter()
        preview: list[dict[str, Any]] = []
        total = 0
        columns: list[str] = []
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                if cur.description:
                    columns = [d.name if hasattr(d, "name") else d[0] for d in cur.description]
                    while True:
                        batch = cur.fetchmany(10_000)
                        if not batch:
                            break
                        for row in batch[: max(0, preview_rows - len(preview))]:
                            preview.append({c: _jsonable(v) for c, v in zip(columns, row, strict=False)})
                        total += len(batch)
        except Exception as exc:
            raise EngineError(str(exc).splitlines()[0]) from exc
        finally:
            if timeout_s:
                try:
                    self._run("SET statement_timeout = 0")
                except EngineError:
                    pass
        return ExecResult(columns=columns, preview=preview, rows=total,
                          elapsed_s=time.perf_counter() - start, cost_usd=0.0, truncated=total > len(preview))

    text_type = "TEXT"

    def md5_int(self, text_expr: str) -> str:
        return f"(('x' || SUBSTR(MD5({text_expr}), 1, 7))::bit(28)::bigint)"

    def sample_clause(self, fraction: float, seed: int, method: str) -> str:
        from ..sampling import percent_text

        kind = "BERNOULLI" if method == "bernoulli" else "SYSTEM"
        return f"TABLESAMPLE {kind} ({percent_text(fraction)}) REPEATABLE ({int(seed)})"


def application_name(tags: dict[str, str]) -> str:
    """gulliver:<receipt or kind>:<agent>, ASCII, within Postgres' 63-byte limit."""
    text = ":".join(["gulliver", tags.get("receipt") or tags.get("kind") or "query", tags.get("agent") or ""])
    return text.encode("ascii", "replace").decode()[:63].rstrip(":")


def estimate_from_plan(plan_doc: Any, relation_bytes) -> Estimate:
    """Turn EXPLAIN (FORMAT JSON) output into an Estimate. Split out so it can be tested without a server."""
    if isinstance(plan_doc, list) and plan_doc:
        plan_doc = plan_doc[0]
    root = (plan_doc or {}).get("Plan", {}) if isinstance(plan_doc, dict) else {}
    scanned = 0
    seen: set[tuple[str | None, str]] = set()

    def walk(node: dict[str, Any]) -> None:
        nonlocal scanned
        node_type = node.get("Node Type", "")
        relation = node.get("Relation Name")
        if relation and ("Seq Scan" in node_type or "Sample Scan" in node_type):
            key = (node.get("Schema"), relation)
            if key not in seen:
                seen.add(key)
                size = relation_bytes(node.get("Schema"), relation)
                if size:
                    scanned += size
        elif relation and "Index" in node_type:
            scanned += int(node.get("Plan Rows", 0)) * int(node.get("Plan Width", 0))
        for child in node.get("Plans", []) or []:
            walk(child)

    if root:
        walk(root)
    return Estimate(
        bytes=scanned or None,
        rows=int(root.get("Plan Rows")) if root.get("Plan Rows") is not None else None,
        cost_usd=0.0,
        accuracy=HEURISTIC,
        source="postgres EXPLAIN (planner statistics)",
        notes=[f"planner cost units: {root.get('Total Cost')}"] if root.get("Total Cost") is not None else [],
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
