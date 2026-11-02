"""DuckDB: local files, Parquet/CSV and in-memory data. Free to run, so the budget is time and bytes."""

from __future__ import annotations

import glob
import json
import os
import threading
import time
from typing import Any

import sqlglot
from sqlglot import exp

from ..config import ConfigError, Connection
from .base import HEURISTIC, Engine, EngineError, Estimate, ExecResult, TableInfo

TYPE_BYTES = {
    "BOOLEAN": 1, "TINYINT": 1, "UTINYINT": 1, "SMALLINT": 2, "USMALLINT": 2,
    "INTEGER": 4, "UINTEGER": 4, "FLOAT": 4, "DATE": 4, "BIGINT": 8, "UBIGINT": 8,
    "DOUBLE": 8, "TIMESTAMP": 8, "TIMESTAMP WITH TIME ZONE": 8, "TIME": 8, "INTERVAL": 16,
    "HUGEINT": 16, "UHUGEINT": 16, "UUID": 16, "VARCHAR": 24, "BLOB": 32,
}


def type_width(data_type: str) -> int:
    t = (data_type or "").upper()
    if t in TYPE_BYTES:
        return TYPE_BYTES[t]
    if t.startswith("DECIMAL"):
        return 16
    if t.startswith(("VARCHAR", "STRING", "TEXT")):
        return 24
    if t.endswith("[]") or t.startswith(("STRUCT", "MAP", "LIST", "UNION")):
        return 64
    return 8


class DuckDBEngine(Engine):
    name = "duckdb"
    dialect = "duckdb"
    billed = False

    def __init__(self, connection: Connection, read_only: bool = True):
        super().__init__(connection, read_only)
        try:
            import duckdb  # noqa: F401
        except ImportError as exc:  # pragma: no cover - duckdb is a core dependency
            raise EngineError("duckdb is not installed: pip install duckdb") from exc
        import duckdb

        path = str(self.params.get("path") or ":memory:")
        self.path = path
        is_file = path != ":memory:"
        if is_file and read_only and not os.path.exists(path):
            raise EngineError(f"DuckDB file not found: {path}")
        try:
            self.con = duckdb.connect(path, read_only=bool(is_file and read_only))
        except duckdb.Error as exc:
            raise EngineError(f"Could not open DuckDB database {path}: {exc}") from exc
        threads = self.params.get("threads")
        if threads:
            try:
                self.con.execute(f"SET threads = {int(threads)}")
            except duckdb.Error:  # already set and locked by an earlier connection to the same file
                pass
        from ..security import sandbox_duckdb

        try:  # file and network access limited to allowed_paths, then the settings are locked
            self.sandbox = sandbox_duckdb(self.con, connection)
        except (duckdb.Error, ConfigError) as exc:
            self.con.close()
            raise EngineError(f"Could not sandbox DuckDB database {path}: {_clean_error(exc)}") from exc

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:  # pragma: no cover
            pass

    # -- metadata ------------------------------------------------------------------
    def _columns(self, database: str, schema: str, table: str) -> list[dict[str, Any]]:
        rows = self.con.execute(
            "SELECT column_name, data_type FROM duckdb_columns() "
            "WHERE database_name = ? AND schema_name = ? AND table_name = ? ORDER BY column_index",
            [database, schema, table],
        ).fetchall()
        return [{"name": r[0], "type": r[1]} for r in rows]

    def _table_from_row(self, database: str, schema: str, table: str, rows: int | None) -> TableInfo:
        columns = self._columns(database, schema, table)
        width = sum(type_width(c["type"]) for c in columns) or 8
        name = table if schema == "main" else f"{schema}.{table}"
        info = TableInfo(name=name, rows=rows, bytes=(rows or 0) * width, columns=columns)
        info.notes.append("bytes are estimated from row count x column widths")
        return info

    def list_tables(self, limit: int = 50) -> list[TableInfo]:
        rows = self.con.execute(
            "SELECT database_name, schema_name, table_name, estimated_size FROM duckdb_tables() "
            "WHERE NOT internal ORDER BY estimated_size DESC NULLS LAST LIMIT ?",
            [int(limit)],
        ).fetchall()
        out = [self._table_from_row(d, s, t, n) for d, s, t, n in rows]
        views = self.con.execute(
            "SELECT schema_name, view_name FROM duckdb_views() WHERE NOT internal LIMIT ?", [int(limit)]
        ).fetchall()
        for schema, view in views:
            name = view if schema == "main" else f"{schema}.{view}"
            out.append(TableInfo(name=name, kind="view", notes=["views have no stored size"]))
        return out[:limit]

    def table_info(self, ref: str) -> TableInfo | None:
        try:
            node = sqlglot.parse_one(f"SELECT * FROM {ref}", read="duckdb").find(exp.Table)
        except sqlglot.errors.ParseError:
            return None
        if node is None:
            return None
        if not isinstance(node.this, exp.Identifier):
            return self._file_info(node, ref)
        name, schema, catalog = node.name, node.db or None, node.catalog or None
        query = ("SELECT database_name, schema_name, table_name, estimated_size FROM duckdb_tables() "
                 "WHERE lower(table_name) = lower(?)")
        params: list[Any] = [name]
        if schema:
            query += " AND lower(schema_name) = lower(?)"
            params.append(schema)
        if catalog:
            query += " AND lower(database_name) = lower(?)"
            params.append(catalog)
        row = self.con.execute(query + " LIMIT 1", params).fetchone()
        if row is None:
            return None
        info = self._table_from_row(*row)
        info.name = ref
        return info

    def view_sources(self, ref: str) -> list[str] | None:
        """The tables a view reads (from its definition), or None when ref is not a view."""
        try:
            node = sqlglot.parse_one(f"SELECT * FROM {ref}", read="duckdb").find(exp.Table)
        except sqlglot.errors.ParseError:
            return None
        if node is None or not isinstance(node.this, exp.Identifier):
            return None
        query = "SELECT sql FROM duckdb_views() WHERE NOT internal AND lower(view_name) = lower(?)"
        params: list[Any] = [node.name]
        if node.db:
            query += " AND lower(schema_name) = lower(?)"
            params.append(node.db)
        row = self.con.execute(query + " LIMIT 1", params).fetchone()
        if row is None or not row[0]:
            return None
        from ..sqlsafety import classify

        try:
            body = sqlglot.parse_one(str(row[0]), read="duckdb").expression
        except sqlglot.errors.ParseError:
            return None
        if body is None:
            return None
        return list(classify(body.sql(dialect="duckdb"), "duckdb").tables)

    def _file_info(self, node: exp.Table, ref: str) -> TableInfo | None:
        func = node.this
        args = [a for a in func.iter_expressions() if isinstance(a, exp.Literal) and a.is_string]
        if not args:
            return TableInfo(name=ref, kind="file", notes=["could not read the file path"])
        path = args[0].this
        from ..security import check_path, policy_for

        refused = check_path(path, policy_for(self.connection))
        if refused is not None:  # do not even reveal whether the file exists
            return TableInfo(name=ref, kind="file", notes=[f"{refused.label}: {refused.reason}"])
        files = sorted(glob.glob(path)) if any(ch in path for ch in "*?[") else [path]
        files = [f for f in files if os.path.exists(f)]
        size = sum(os.path.getsize(f) for f in files) if files else None
        info = TableInfo(name=ref, bytes=size, kind="file")
        fname = type(func).__name__.lower()
        if "parquet" in fname or path.lower().endswith(".parquet"):
            try:
                rows = self.con.execute("SELECT SUM(num_rows) FROM parquet_file_metadata(?)", [path]).fetchone()
                info.rows = int(rows[0]) if rows and rows[0] is not None else None
            except Exception:
                info.notes.append("could not read Parquet metadata")
        else:
            info.notes.append("row count unknown for this file type; bytes = file size")
        if not files:
            info.notes.append("no files matched the path")
        return info

    def data_version(self, tables: list[str]) -> str | None:
        """File stamps (mtime + size) of the database file, its WAL and any files read directly.
        None for in-memory databases, views, attached databases and remote files: no reliable signal."""
        if self.path == ":memory:" or not tables:
            return None
        stamps: list[str] = []
        uses_db = False
        main_db = (self.con.execute("SELECT current_database()").fetchone() or [""])[0]
        for ref in tables:
            try:
                node = sqlglot.parse_one(f"SELECT * FROM {ref}", read="duckdb").find(exp.Table)
            except sqlglot.errors.ParseError:
                return None
            if node is None:
                return None
            if not isinstance(node.this, exp.Identifier):
                literals = [a for a in node.this.iter_expressions() if isinstance(a, exp.Literal) and a.is_string]
                if len(literals) != 1:
                    return None
                file_stamps = _file_stamps(literals[0].this)
                if file_stamps is None:
                    return None
                stamps += file_stamps
                continue
            if node.catalog and node.catalog.lower() != str(main_db).lower():
                return None
            view = self.con.execute("SELECT 1 FROM duckdb_views() WHERE NOT internal AND lower(view_name) = lower(?)",
                                    [node.name]).fetchone()
            if view:
                return None
            query = ("SELECT 1 FROM duckdb_tables() WHERE lower(database_name) = lower(?) "
                     "AND lower(table_name) = lower(?)")
            params: list[Any] = [str(main_db), node.name]
            if node.db:
                query += " AND lower(schema_name) = lower(?)"
                params.append(node.db)
            if self.con.execute(query, params).fetchone() is None:
                # not a table of this database file: a replacement scan (FROM 'x.parquet') reads the file itself
                file_stamps = _file_stamps(node.name) if not node.db else None
                if file_stamps is None:
                    return None
                stamps += file_stamps
                continue
            uses_db = True
        if uses_db:
            stamps += [_stamp(self.path), _stamp(self.path + ".wal")]
        return "duckdb:" + "|".join(stamps)

    # -- estimation ----------------------------------------------------------------
    def _plan(self, sql: str) -> list[dict[str, Any]]:
        try:
            row = self.con.execute(f"EXPLAIN (FORMAT JSON) {sql}").fetchall()
        except Exception as exc:
            raise EngineError(_clean_error(exc)) from exc
        text = row[0][1] if row and len(row[0]) > 1 else "[]"
        try:
            plan = json.loads(text)
        except json.JSONDecodeError:
            return []
        return plan if isinstance(plan, list) else [plan]

    def estimate(self, sql: str) -> Estimate:
        plan = self._plan(sql)
        root_rows = _first_cardinality(plan)
        scanned = 0
        notes: list[str] = []
        scans = list(_walk(plan))
        table_cache: dict[str, TableInfo | None] = {}
        for node in scans:
            extra = node.get("extra_info") or {}
            if not isinstance(extra, dict):
                continue
            table_name = extra.get("Table")
            projections = extra.get("Projections") or []
            if isinstance(projections, str):
                projections = [projections]
            if table_name:
                short = ".".join(table_name.split(".")[1:]) if table_name.count(".") >= 2 else table_name
                key = short.removeprefix("main.")
                if key not in table_cache:
                    table_cache[key] = self.table_info(key)
                info = table_cache[key]
                if info and info.rows is not None and info.columns:
                    widths = {c["name"].lower(): type_width(c["type"]) for c in info.columns}
                    used = sum(widths.get(str(p).lower(), 8) for p in projections) if projections else sum(widths.values())
                    scanned += info.rows * used
            elif str(extra.get("Function", "")).upper().startswith(("READ_PARQUET", "PARQUET_SCAN", "READ_CSV")):
                card = _int(extra.get("Estimated Cardinality"))
                if card:
                    scanned += card * 8 * max(1, len(projections))
                    notes.append("file scans: bytes estimated from estimated rows x projected columns")
        return Estimate(
            bytes=scanned or None,
            rows=root_rows,
            cost_usd=0.0,
            accuracy=HEURISTIC,
            source="duckdb EXPLAIN + table metadata",
            notes=notes + ["DuckDB runs locally: no money is charged; time and memory are the budget."],
        )

    # -- execution -----------------------------------------------------------------
    def execute(self, sql: str, preview_rows: int, timeout_s: float | None = None,
                max_bytes_billed: int | None = None) -> ExecResult:
        timer = None
        timed_out = {"hit": False}
        if timeout_s:
            def _stop() -> None:
                timed_out["hit"] = True
                try:
                    self.con.interrupt()
                except Exception:  # pragma: no cover
                    pass
            timer = threading.Timer(timeout_s, _stop)
            timer.daemon = True
            timer.start()
        start = time.perf_counter()
        try:
            cursor = self.con.execute(sql)
            columns = [d[0] for d in (cursor.description or [])]
            preview: list[dict[str, Any]] = []
            total = 0
            if columns:
                while True:
                    batch = cursor.fetchmany(10_000)
                    if not batch:
                        break
                    if len(preview) < preview_rows:
                        for row in batch[: preview_rows - len(preview)]:
                            preview.append({c: _jsonable(v) for c, v in zip(columns, row, strict=False)})
                    total += len(batch)
        except Exception as exc:
            if timed_out["hit"]:
                raise EngineError(f"Stopped after {timeout_s:.0f}s (runtime limit).") from exc
            raise EngineError(_clean_error(exc)) from exc
        finally:
            if timer:
                timer.cancel()
        elapsed = time.perf_counter() - start
        return ExecResult(columns=columns, preview=preview, rows=total, elapsed_s=elapsed,
                          bytes_processed=None, cost_usd=0.0, truncated=total > len(preview))

    def sample_clause(self, fraction: float, seed: int, method: str) -> str:
        from ..sampling import percent_text

        method = "bernoulli" if method == "bernoulli" else "system"
        return f"TABLESAMPLE {percent_text(fraction)}% ({method}, {int(seed)})"


def _file_stamps(path: str) -> list[str] | None:
    """Stamps of the local file(s) a path or glob names; None when any is missing or not a plain local file."""
    files = sorted(glob.glob(path)) if any(ch in path for ch in "*?[") else [path]
    if not files or not all(os.path.isfile(f) for f in files):
        return None
    return [_stamp(f) for f in files]


def _stamp(path: str) -> str:
    try:
        st = os.stat(path)
    except OSError:
        return f"{path}:none"
    return f"{path}:{st.st_mtime_ns}:{st.st_size}"


def _walk(nodes: list[dict[str, Any]]):
    for node in nodes:
        if not isinstance(node, dict):
            continue
        yield node
        yield from _walk(node.get("children") or [])


def _first_cardinality(nodes: list[dict[str, Any]]) -> int | None:
    for node in _walk(nodes):
        extra = node.get("extra_info") or {}
        if isinstance(extra, dict) and "Estimated Cardinality" in extra:
            return _int(extra["Estimated Cardinality"])
    return None


def _int(value: Any) -> int | None:
    try:
        return int(str(value).replace("~", "").strip())
    except (TypeError, ValueError):
        return None


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _clean_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__
