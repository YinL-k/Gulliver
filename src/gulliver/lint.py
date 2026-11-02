"""Scale lint: SQL patterns that work on small data and hurt on big data.

Every rule has a stable id (GV001...), a severity and a concrete fix.
  error  blocks gulliver run (fix the SQL first)
  warn   allowed, but shown in the preflight and the receipt
  info   a cheaper alternative exists
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlglot import exp

from .sqlsafety import base_tables, table_ref

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}

RULES = {
    "GV001": "select-star",
    "GV002": "unbounded-output",
    "GV003": "cartesian-join",
    "GV004": "missing-partition-filter",
    "GV005": "function-on-filter-column",
    "GV006": "order-without-limit",
    "GV007": "exact-count-distinct",
    "GV008": "non-equi-join",
    "GV009": "union-distinct",
    "GV010": "scalar-subquery-in-select",
    "GV011": "window-without-partition",
    "GV012": "not-in-subquery",
    "GV014": "sensitive-column-in-output",
}


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    fix: str

    @property
    def name(self) -> str:
        return RULES.get(self.rule, self.rule)

    def as_dict(self) -> dict:
        return {"rule": self.rule, "name": self.name, **{k: v for k, v in asdict(self).items() if k != "rule"}}


AGG_TYPES = (exp.AggFunc,)
COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In, exp.Like, exp.ILike)


def _outer_select(tree: exp.Expression) -> exp.Select | None:
    if isinstance(tree, exp.Select):
        return tree
    if isinstance(tree, exp.Subquery):
        return _outer_select(tree.this)
    return None


def _has_aggregation(select: exp.Select) -> bool:
    if select.args.get("group"):
        return True
    for projection in select.expressions:
        for node in projection.walk():
            if isinstance(node, AGG_TYPES) and not node.find_ancestor(exp.Window):
                return True
    return False


def _is_star_projection(node: exp.Expression) -> bool:
    return isinstance(node, exp.Star) or (isinstance(node, exp.Column) and isinstance(node.this, exp.Star))


def _join_condition(join: exp.Join) -> exp.Expression | None:
    return join.args.get("on") or (exp.true() if join.args.get("using") else None)


def _aliases_in(expr: exp.Expression) -> set[str]:
    return {col.table.lower() for col in expr.find_all(exp.Column) if col.table}


def lint(statement_tree: exp.Expression, dialect: str = "duckdb",
         table_info: dict[str, dict] | None = None) -> list[Finding]:
    """Lint a read tree (the SELECT part). `table_info` maps lower-case table refs to engine
    metadata; when it says a table is partitioned, GV004 can check the filter."""
    tree = statement_tree
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()

    def add(rule: str, severity: str, message: str, fix: str) -> None:
        key = (rule, message)
        if key not in seen:
            seen.add(key)
            findings.append(Finding(rule, severity, message, fix))

    outer = _outer_select(tree)

    # GV001 select-star (only where it reaches the output or a scan; COUNT(*) is fine)
    for select in tree.find_all(exp.Select):
        if any(_is_star_projection(p) for p in select.expressions):
            add("GV001", "warn",
                "SELECT * reads every column. Columnar engines (BigQuery, Snowflake, DuckDB on Parquet) "
                "charge and work per column.",
                "List only the columns you need.")
            break

    # GV002 unbounded-output and GV006 order-without-limit on the outermost query
    if outer is not None:
        has_limit = bool(outer.args.get("limit") or outer.args.get("fetch"))
        if not has_limit and not _has_aggregation(outer):
            add("GV002", "warn",
                "The result has no LIMIT and no aggregation, so it can return every row.",
                "Add a LIMIT for exploration, or aggregate (GROUP BY / COUNT) before returning rows.")
        if outer.args.get("order") and not has_limit:
            add("GV006", "warn",
                "ORDER BY without LIMIT sorts the whole result.",
                "Add a LIMIT, or drop the ORDER BY if the order does not matter.")
    for select in tree.find_all(exp.Select):
        if select is outer:
            continue
        if select.args.get("order") and not select.args.get("limit") and not select.find_ancestor(exp.Window):
            add("GV006", "info",
                "ORDER BY inside a subquery without LIMIT is usually ignored but may still cost a sort.",
                "Remove the inner ORDER BY.")

    # GV003 cartesian-join / GV008 non-equi-join
    for select in tree.find_all(exp.Select):
        where = select.args.get("where")
        where_aliases = _aliases_in(where) if where is not None else set()
        for join in select.args.get("joins") or []:
            target = join.this
            if isinstance(target, (exp.Unnest, exp.Lateral)) or (
                isinstance(target, exp.Table) and isinstance(target.this, exp.Unnest)
            ):
                continue
            alias = (target.alias_or_name or "").lower()
            kind = (join.args.get("kind") or "").upper()
            condition = _join_condition(join)
            if kind == "CROSS":
                add("GV003", "warn",
                    f"CROSS JOIN with {alias or 'a table'} multiplies row counts (rows A x rows B).",
                    "Make sure one side is tiny, or join on a key instead.")
                continue
            if condition is None:
                joined_in_where = where is not None and alias in where_aliases and any(
                    isinstance(eq, exp.EQ) for eq in where.find_all(exp.EQ)
                )
                if joined_in_where:
                    add("GV003", "info",
                        f"Comma join with {alias} relies on the WHERE clause for its join key.",
                        "Write it as an explicit JOIN ... ON so a missing key cannot slip in.")
                else:
                    add("GV003", "error",
                        f"Join with {alias or 'a table'} has no join condition: a cartesian product.",
                        "Add ON <key> = <key> (or USING). If you really want every combination, write CROSS JOIN.")
                continue
            if join.args.get("on") is not None:
                on = join.args["on"]
                has_eq = any(True for _ in on.find_all(exp.EQ))
                top_or = isinstance(on, exp.Or)
                if not has_eq or top_or:
                    add("GV008", "warn",
                        f"Join with {alias} uses a non-equality or OR condition; these joins can explode "
                        "and cannot use hash joins.",
                        "Join on equality keys where possible; split OR conditions into UNION ALL.")

    # GV004 missing-partition-filter (needs engine metadata)
    if table_info:
        where_columns = {
            col.name.lower()
            for select in tree.find_all(exp.Select)
            if select.args.get("where") is not None
            for col in select.args["where"].find_all(exp.Column)
        }
        for table in base_tables(tree, dialect):
            info = table_info.get(table_ref(table, dialect).lower())
            if not info:
                continue
            part = (info.get("partitioned_by") or "")
            part_cols = [p.strip().lower() for p in str(part).split(",") if p.strip()]
            if part_cols and not any(p in where_columns for p in part_cols):
                add("GV004", "error",
                    f"{table_ref(table, dialect)} is partitioned by {', '.join(part_cols)} but the query does not "
                    "filter on it, so every partition is scanned.",
                    f"Add a WHERE filter on {part_cols[0]} (for example a date range).")

    # GV005 function-on-filter-column
    for select in tree.find_all(exp.Select):
        where = select.args.get("where")
        if where is None:
            continue
        for cmp in where.find_all(*COMPARISONS):
            operands = [cmp.this, cmp.args.get("expression")]
            for operand in operands:
                if isinstance(operand, (exp.Func, exp.Cast, exp.Binary)) and not isinstance(operand, exp.Column):
                    if isinstance(operand, (exp.Binary,)) and not isinstance(operand, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
                        continue
                    cols = list(operand.find_all(exp.Column))
                    if cols and not isinstance(operand, exp.AggFunc):
                        add("GV005", "warn",
                            f"The filter wraps {cols[0].sql(dialect=dialect)} in a function or calculation, which "
                            "blocks partition pruning and min/max skipping.",
                            "Compare the raw column to a constant range, e.g. ts >= '2024-01-01' AND "
                            "ts < '2024-01-02' instead of DATE(ts) = '2024-01-01'.")
                        break

    # GV007 exact-count-distinct
    for count in tree.find_all(exp.Count):
        if isinstance(count.this, exp.Distinct):
            add("GV007", "info",
                "COUNT(DISTINCT ...) is exact and memory-heavy on big data.",
                "If an estimate is fine, use APPROX_COUNT_DISTINCT (or the engine's HLL function).")
            break

    # GV009 union-distinct
    for union in tree.find_all(exp.Union):
        if union.args.get("distinct"):
            add("GV009", "info",
                "UNION removes duplicates, which needs a full sort or hash of both sides.",
                "Use UNION ALL if duplicates are impossible or acceptable.")
            break

    # GV010 scalar subquery in the SELECT list
    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            if any(isinstance(n, exp.Subquery) for n in projection.walk()):
                add("GV010", "info",
                    "A subquery in the SELECT list can run once per row on some engines.",
                    "Rewrite it as a JOIN on a pre-aggregated subquery.")
                break

    # GV011 window-without-partition
    for window in tree.find_all(exp.Window):
        if not window.args.get("partition_by") and window.args.get("order"):
            add("GV011", "warn",
                "A window function with ORDER BY but no PARTITION BY puts every row in one partition "
                "(one worker, one big sort).",
                "Add PARTITION BY a key, or compute it on a smaller result.")
            break

    # GV012 NOT IN (subquery)
    for not_node in tree.find_all(exp.Not):
        inner = not_node.this
        if isinstance(inner, exp.In) and inner.args.get("query") is not None:
            add("GV012", "info",
                "NOT IN (subquery) returns nothing if the subquery has a NULL, and often plans badly.",
                "Use NOT EXISTS or a LEFT JOIN ... WHERE key IS NULL.")
            break

    # GV014 sensitive-column-in-output (column names, lineage, default policy; preflight adds the connection policy)
    from .guard import lint_findings

    for severity, message, fix in lint_findings(tree, dialect, table_info):
        add("GV014", severity, message, fix)

    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.rule))
    return findings


def summarize(findings: list[Finding]) -> dict[str, int]:
    out = {"error": 0, "warn": 0, "info": 0}
    for f in findings:
        out[f.severity] += 1
    return out
