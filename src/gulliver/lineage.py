"""Blast radius: which models and columns depend on a table or column.

Reads a folder of SQL models (plain SQL, or dbt-style with {{ ref() }} / {{ source() }}) or a dbt
manifest.json, builds the model graph and column-level lineage with sqlglot, and answers:
  - impact: "if raw.events.user_id changes or disappears, what breaks downstream?"
  - drift:  schema snapshot of a connection, compared with the live schema, mapped to impacted models
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import lineage as sqlglot_lineage

REF_RE = re.compile(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"](?:\s*,\s*['\"]([^'\"]+)['\"])?\s*(?:,[^)]*)?\)\s*\}\}")
SOURCE_RE = re.compile(r"\{\{\s*source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
THIS_RE = re.compile(r"\{\{\s*this\s*\}\}")
CONFIG_RE = re.compile(r"\{\{\s*config\((?:[^{}]|\{[^{}]*\})*\)\s*\}\}", re.S)
BLOCK_RE = re.compile(r"\{%-?.*?-?%\}", re.S)
COMMENT_RE = re.compile(r"\{#.*?#\}", re.S)
EXPR_RE = re.compile(r"\{\{.*?\}\}", re.S)


@dataclass
class Model:
    name: str
    sql: str
    path: str = ""
    refs: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    tables: set[str] = field(default_factory=set)
    columns: dict[str, set[tuple[str, str]]] = field(default_factory=dict)
    star_from: set[str] = field(default_factory=set)
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "upstream": sorted(self.tables),
                               "columns": sorted(self.columns)}
        if self.path:
            out["path"] = self.path
        if self.star_from:
            out["select_star_from"] = sorted(self.star_from)
        if self.error:
            out["error"] = self.error
        return out


def render(text: str, model_name: str = "this") -> tuple[str, set[str], set[str]]:
    """Strip the Jinja that dbt models use, keep the SQL. Returns (sql, refs, sources)."""
    refs: set[str] = set()
    sources: set[str] = set()

    def ref(m: re.Match) -> str:
        name = m.group(2) or m.group(1)
        refs.add(name.lower())
        return name

    def source(m: re.Match) -> str:
        sources.add(f"{m.group(1)}.{m.group(2)}".lower())
        return f"{m.group(1)}.{m.group(2)}"

    sql = COMMENT_RE.sub("", text)
    sql = CONFIG_RE.sub("", sql)
    sql = REF_RE.sub(ref, sql)
    sql = SOURCE_RE.sub(source, sql)
    sql = THIS_RE.sub(model_name, sql)
    sql = BLOCK_RE.sub("", sql)
    sql = EXPR_RE.sub("NULL", sql)
    return sql.strip().rstrip(";"), refs, sources


def table_name(table: exp.Table) -> str:
    parts = [p for p in (table.catalog, table.db, table.name) if p]
    return ".".join(parts).lower()


def load_models(models_dir: str | None = None, manifest: str | None = None) -> dict[str, Model]:
    models: dict[str, Model] = {}
    if manifest:
        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
        for node in (data.get("nodes") or {}).values():
            if node.get("resource_type") != "model":
                continue
            code = node.get("compiled_code") or node.get("compiled_sql") or node.get("raw_code") or ""
            name = node["name"].lower()
            sql, refs, sources = render(code, name)
            for dep in (node.get("depends_on") or {}).get("nodes") or []:
                kind, _, rest = dep.partition(".")
                if kind == "model":
                    refs.add(rest.split(".")[-1].lower())
            models[name] = Model(name=name, sql=sql, path=node.get("original_file_path", ""), refs=refs,
                                 sources=sources)
    if models_dir:
        root = Path(models_dir)
        if not root.is_dir():
            raise FileNotFoundError(f"Models folder not found: {models_dir}")
        for path in sorted(root.rglob("*.sql")):
            name = path.stem.lower()
            sql, refs, sources = render(path.read_text(encoding="utf-8"), name)
            models[name] = Model(name=name, sql=sql, path=str(path.relative_to(root)), refs=refs, sources=sources)
    return models


def _topo(models: dict[str, Model]) -> tuple[list[str], list[str]]:
    indeg = {n: 0 for n in models}
    children: dict[str, list[str]] = {n: [] for n in models}
    for name, model in models.items():
        for up in model.tables:
            if up in models and up != name:
                indeg[name] += 1
                children[up].append(name)
    queue = deque(sorted(n for n, d in indeg.items() if d == 0))
    order: list[str] = []
    while queue:
        n = queue.popleft()
        order.append(n)
        for child in sorted(children[n]):
            indeg[child] -= 1
            if indeg[child] == 0:
                queue.append(child)
    cyclic = sorted(n for n, d in indeg.items() if d > 0)
    return order + cyclic, cyclic


def build(models: dict[str, Model], dialect: str = "duckdb",
          schema: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """Parse every model, find its upstream tables and column lineage. Returns graph info."""
    known: dict[str, dict[str, str]] = {k.lower(): dict(v) for k, v in (schema or {}).items() if "." not in k}
    for model in models.values():
        try:
            tree = sqlglot.parse_one(model.sql, read=dialect)
        except SqlglotError as exc:
            model.error = f"parse error: {str(exc).splitlines()[0]}"
            continue
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        model.tables = {table_name(t) for t in tree.find_all(exp.Table)
                        if isinstance(t.this, exp.Identifier) and table_name(t) not in ctes}
        for select in tree.find_all(exp.Select):
            if any(isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
                   for p in select.expressions):
                for t in select.find_all(exp.Table):
                    if t.find_ancestor(exp.Select) is select and table_name(t) not in ctes:
                        model.star_from.add(table_name(t))

    order, cyclic = _topo(models)
    for name in order:
        model = models[name]
        if model.error:
            continue
        try:
            nodes = sqlglot_lineage(None, model.sql, known or None, dialect=dialect)
        except Exception as exc:  # sqlglot raises many kinds of errors on unusual SQL
            try:
                nodes = sqlglot_lineage(None, model.sql, None, dialect=dialect)
            except Exception:
                model.error = f"lineage failed: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}"
                continue
        if isinstance(nodes, dict):
            items = nodes.items()
        else:  # pragma: no cover - single column result
            items = [(nodes.name, nodes)]
        for out_col, node in items:
            leaves: set[tuple[str, str]] = set()
            for n in node.walk():
                if n.downstream:
                    continue
                if isinstance(n.source, exp.Table):
                    leaves.add((table_name(n.source), n.name.split(".")[-1].lower()))
            model.columns[out_col.lower()] = leaves
        known[name] = {c: "UNKNOWN" for c in model.columns if c != "*"}

    edges = sorted({(up, m.name) for m in models.values() for up in m.tables})
    return {"models": len(models), "edges": [{"from": a, "to": b} for a, b in edges], "order": order,
            "cycles": cyclic, "errors": {m.name: m.error for m in models.values() if m.error}}


def _table_matches(candidate: str, target: str) -> bool:
    return candidate == target or candidate.endswith("." + target) or target.endswith("." + candidate)


def parse_target(target: str) -> tuple[str, str]:
    """'raw.events.user_id' -> ('raw.events', 'user_id'); 'events' -> ('events', '*')."""
    parts = [p.strip().strip('`"').lower() for p in target.split(".") if p.strip()]
    if not parts:
        raise ValueError("Give a table or table.column")
    if len(parts) == 1:
        return parts[0], "*"
    return ".".join(parts[:-1]), parts[-1]


def impact(models: dict[str, Model], targets: list[tuple[str, str]], tables_only: set[str] | None = None) -> dict[str, Any]:
    """Downstream models and columns reached from (table, column) targets. column '*' = the whole table."""
    frontier = deque(targets)
    seen: set[tuple[str, str]] = set(targets)
    hits: dict[str, dict[str, Any]] = {}

    def hit(model: Model, column: str | None, via: str, depth: int) -> None:
        entry = hits.setdefault(model.name, {"model": model.name, "path": model.path, "columns": set(), "via": set(),
                                             "depth": depth, "select_star": False})
        entry["depth"] = min(entry["depth"], depth)
        entry["via"].add(via)
        if column:
            entry["columns"].add(column)

    depth_of = {t: 0 for t in targets}
    while frontier:
        table, column = frontier.popleft()
        depth = depth_of[(table, column)] + 1
        for model in models.values():
            if not any(_table_matches(t, table) for t in model.tables):
                continue
            via = f"{table}.{column}" if column != "*" else table
            if any(_table_matches(t, table) for t in model.star_from):
                hit(model, None, via, depth)
                hits[model.name]["select_star"] = True
            matched_cols = [out for out, leaves in model.columns.items()
                            if any(_table_matches(lt, table) and (column == "*" or lc == column) for lt, lc in leaves)]
            if column == "*" and not matched_cols:
                hit(model, None, via, depth)
            for out in matched_cols:
                hit(model, out, via, depth)
                nxt = (model.name, out)
                if nxt not in seen:
                    seen.add(nxt)
                    depth_of[nxt] = depth
                    frontier.append(nxt)
            if column == "*" and (model.name, "*") not in seen:
                nxt = (model.name, "*")
                seen.add(nxt)
                depth_of[nxt] = depth
                frontier.append(nxt)
            # a column used only in filters / joins does not show up in output lineage: still a dependency
            if column != "*" and not matched_cols and _uses_column(model, table, column):
                hit(model, None, via + " (filter/join)", depth)
    out = []
    for entry in sorted(hits.values(), key=lambda e: (e["depth"], e["model"])):
        entry["columns"] = sorted(entry["columns"])
        entry["via"] = sorted(entry["via"])
        out.append(entry)
    return {"affected": out, "count": len(out)}


def _uses_column(model: Model, table: str, column: str) -> bool:
    try:
        tree = sqlglot.parse_one(model.sql)
    except SqlglotError:
        return False
    aliases = {t.alias_or_name.lower(): table_name(t) for t in tree.find_all(exp.Table)}
    for col in tree.find_all(exp.Column):
        if col.name.lower() != column:
            continue
        qual = col.table.lower()
        if not qual:
            if any(_table_matches(t, table) for t in model.tables):
                return True
        elif _table_matches(aliases.get(qual, qual), table):
            return True
    return False


# -- schema snapshots and drift ----------------------------------------------------------
def snapshot_from_engine(engine: Any, limit: int = 1000) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for info in engine.list_tables(limit=limit):
        columns = info.columns
        if not columns:
            detailed = engine.table_info(info.name)
            columns = detailed.columns if detailed else None
        tables[info.name.lower()] = {
            "rows": info.rows,
            "kind": info.kind,
            "columns": {c["name"].lower(): str(c["type"]) for c in (columns or [])},
        }
    return tables


def compare(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changes: list[dict[str, Any]] = []
    for name in sorted(set(old) & set(new)):
        o, n = old[name], new[name]
        oc, nc = o.get("columns") or {}, n.get("columns") or {}
        for col in sorted(set(nc) - set(oc)):
            changes.append({"table": name, "column": col, "change": "added", "type": nc[col]})
        for col in sorted(set(oc) - set(nc)):
            changes.append({"table": name, "column": col, "change": "removed", "type": oc[col]})
        for col in sorted(set(oc) & set(nc)):
            if oc[col] != nc[col]:
                changes.append({"table": name, "column": col, "change": "type_changed",
                                "from": oc[col], "to": nc[col]})
        orows, nrows = o.get("rows"), n.get("rows")
        if orows and nrows is not None:
            pct = (nrows - orows) / orows * 100
            if abs(pct) >= 20:
                changes.append({"table": name, "change": "row_count", "from": orows, "to": nrows,
                                "percent": round(pct, 1)})
    return {"tables_added": added, "tables_removed": removed, "changes": changes}
