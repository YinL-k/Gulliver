"""Google BigQuery via google-cloud-bigquery.

Connection params:
  project            GCP project that runs (and pays for) the queries
  location           e.g. "EU" or "US" (optional)
  dataset            default dataset for `inspect` without a table (optional)
  price_per_tib_usd  on-demand price per TiB scanned (default 6.25)

Credentials: Google Application Default Credentials (gcloud auth application-default login,
or GOOGLE_APPLICATION_CREDENTIALS). Nothing is stored by Gulliver.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..config import Connection
from .base import EXACT, Engine, EngineError, Estimate, ExecResult, MissingDriver, TableInfo

TIB = 1024**4
MIN_BILLED_PER_TABLE = 10 * 1024**2  # BigQuery bills at least 10 MiB per referenced table


class BigQueryEngine(Engine):
    name = "bigquery"
    dialect = "bigquery"
    billed = True

    def __init__(self, connection: Connection, read_only: bool = True, _client: Any = None):
        super().__init__(connection, read_only)
        self.price_per_tib = float(self.params.get("price_per_tib_usd", 6.25))
        if _client is not None:
            self.client = _client
            self._bq = None
            return
        try:
            from google.cloud import bigquery
        except ImportError as exc:
            raise MissingDriver("BigQuery needs google-cloud-bigquery: pip install 'gulliver-mcp[bigquery]'") from exc
        self._bq = bigquery
        try:
            self.client = bigquery.Client(project=self.params.get("project"), location=self.params.get("location"))
        except Exception as exc:
            raise EngineError(f"Could not create a BigQuery client: {str(exc).splitlines()[0]}") from exc

    def _job_config(self, **kwargs: Any) -> Any:
        if self._bq is None:  # tests: plain dict stands in for QueryJobConfig
            return dict(kwargs)
        return self._bq.QueryJobConfig(**kwargs)

    # -- metadata ------------------------------------------------------------------
    def _info(self, table: Any, ref: str) -> TableInfo:
        part = None
        tp = getattr(table, "time_partitioning", None)
        if tp is not None:
            part = getattr(tp, "field", None) or "_PARTITIONTIME"
        rp = getattr(table, "range_partitioning", None)
        if rp is not None and getattr(rp, "field", None):
            part = rp.field
        clustering = getattr(table, "clustering_fields", None)
        info = TableInfo(
            name=ref,
            rows=getattr(table, "num_rows", None),
            bytes=getattr(table, "num_bytes", None),
            columns=[{"name": f.name, "type": f.field_type} for f in (getattr(table, "schema", None) or [])],
            partitioned_by=part,
            clustered_by=", ".join(clustering) if clustering else None,
            kind=str(getattr(table, "table_type", "TABLE")).lower(),
        )
        if getattr(table, "require_partition_filter", False):
            info.notes.append("BigQuery requires a partition filter on this table")
        return info

    def list_tables(self, limit: int = 50) -> list[TableInfo]:
        dataset = self.params.get("dataset")
        if not dataset:
            raise EngineError("Set 'dataset' on the BigQuery connection, or inspect one table by name.")
        try:
            items = list(self.client.list_tables(dataset, max_results=500))
            tables = [self._info(self.client.get_table(item.reference), f"{item.dataset_id}.{item.table_id}")
                      for item in items[: max(limit, 1) * 2]]
        except Exception as exc:
            raise EngineError(str(exc).splitlines()[0]) from exc
        tables.sort(key=lambda t: t.bytes or 0, reverse=True)
        return tables[:limit]

    def table_info(self, ref: str) -> TableInfo | None:
        clean = ref.strip("`")
        try:
            table = self.client.get_table(clean)
        except Exception:
            return None
        return self._info(table, ref)

    # -- estimation ----------------------------------------------------------------
    def estimate(self, sql: str) -> Estimate:
        try:
            job = self.client.query(sql, job_config=self._job_config(dry_run=True, use_query_cache=False))
        except Exception as exc:
            raise EngineError(_bq_error(exc)) from exc
        processed = int(getattr(job, "total_bytes_processed", 0) or 0)
        referenced = len(getattr(job, "referenced_tables", None) or [])
        billed = max(processed, MIN_BILLED_PER_TABLE * referenced) if referenced else processed
        notes = [f"on-demand price {self.price_per_tib} USD/TiB; with capacity (slot) pricing the money cost differs"]
        if billed != processed:
            notes.append("BigQuery bills at least 10 MiB per referenced table")
        return Estimate(bytes=processed, rows=None, cost_usd=self.cost_for(billed, None), accuracy=EXACT,
                        source="bigquery dry run", notes=notes)

    def cost_for(self, bytes_processed: int | None, runtime_s: float | None) -> float | None:
        if bytes_processed is None:
            return None
        return bytes_processed / TIB * self.price_per_tib

    def max_bytes_for_cost(self, usd: float) -> int | None:
        return int(usd / self.price_per_tib * TIB)

    # -- execution -----------------------------------------------------------------
    def execute(self, sql: str, preview_rows: int, timeout_s: float | None = None,
                max_bytes_billed: int | None = None) -> ExecResult:
        cfg: dict[str, Any] = {"use_query_cache": False}
        if max_bytes_billed:
            cfg["maximum_bytes_billed"] = int(max(max_bytes_billed, MIN_BILLED_PER_TABLE))
        if self.tags:  # job labels show up in INFORMATION_SCHEMA.JOBS and the billing export
            cfg["labels"] = job_labels(self.tags)
        start = time.perf_counter()
        try:
            job = self.client.query(sql, job_config=self._job_config(**cfg))
            result = job.result(timeout=timeout_s, page_size=10_000)
            columns = [f.name for f in (getattr(result, "schema", None) or [])]
            preview: list[dict[str, Any]] = []
            total = 0
            for row in result:
                if len(preview) < preview_rows:
                    values = list(row.values()) if hasattr(row, "values") else list(row)
                    preview.append({c: _jsonable(v) for c, v in zip(columns, values, strict=False)})
                total += 1
        except Exception as exc:
            raise EngineError(_bq_error(exc)) from exc
        billed = getattr(job, "total_bytes_billed", None)
        processed = getattr(job, "total_bytes_processed", None)
        return ExecResult(columns=columns, preview=preview, rows=total, elapsed_s=time.perf_counter() - start,
                          bytes_processed=int(processed) if processed is not None else None,
                          cost_usd=self.cost_for(int(billed), None) if billed is not None else None,
                          truncated=total > len(preview))

    def data_version(self, tables: list[str]) -> str | None:
        """lastModifiedTime + rows + bytes of each base table. None for views, external tables, wildcard
        tables and tables with a streaming buffer (streamed rows do not move lastModifiedTime reliably)."""
        if not tables:
            return None
        parts = []
        for ref in tables:
            try:
                table = self.client.get_table(ref.strip("`"))
            except Exception:
                return None
            kind = str(getattr(table, "table_type", "TABLE") or "TABLE").upper()
            modified = getattr(table, "modified", None)
            if kind != "TABLE" or modified is None or getattr(table, "streaming_buffer", None) is not None:
                return None
            parts.append(f"{ref}:{modified}:{getattr(table, 'num_rows', None)}:{getattr(table, 'num_bytes', None)}")
        return "bigquery:" + "|".join(parts)

    def baseline_latency(self, tries: int = 1) -> float:
        return super().baseline_latency(tries=1)

    def supported_methods(self) -> tuple[str, ...]:
        return ("system",)

    text_type = "STRING"

    def md5_int(self, text_expr: str) -> str:
        return f"CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5({text_expr})), 1, 7)) AS INT64)"

    def query_columns(self, sql: str) -> list[str]:
        try:
            job = self.client.query(sql, job_config=self._job_config(dry_run=True, use_query_cache=False))
        except Exception as exc:
            raise EngineError(_bq_error(exc)) from exc
        return [f.name for f in (getattr(job, "schema", None) or [])]

    def sample_clause(self, fraction: float, seed: int, method: str) -> str:
        from ..sampling import percent_text

        return f"TABLESAMPLE SYSTEM ({percent_text(fraction)} PERCENT)"


def job_labels(tags: dict[str, str]) -> dict[str, str]:
    """BigQuery label rules: lowercase letters, digits, '_' and '-', at most 63 characters."""
    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9_-]+", "-", str(text).lower()).strip("-")[:63]

    labels = {"gulliver": norm(tags.get("kind") or "query")}
    for key in ("agent", "session", "task", "receipt"):
        if tags.get(key):
            labels[f"gulliver_{key}"] = norm(tags[key])
    return labels


def _bq_error(exc: Exception) -> str:
    message = getattr(exc, "message", None) or str(exc)
    text = str(message).splitlines()[0]
    if "bytes billed" in text.lower() and "limit" in text.lower():
        return "Stopped by BigQuery: the query would bill more bytes than the budget allows. " + text
    return text


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
