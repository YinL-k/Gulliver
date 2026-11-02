"""Test gulliver_fix rewrites on generated edge-case data.

`gulliver fix --verify` builds small tables in a private in-memory DuckDB with the columns the query uses,
fills them with seeded draws from edge-case pools (NULL, duplicates, 0/-1, empty strings, leap days, month and
year boundaries, 23:59:59.999999, infinity, daylight-saving jumps), and runs the original and each rewrite
on them. Results are compared as multisets (row order does not count, duplicates do). A difference is shrunk
to a few rows and returned, so a human can see it.

What this proves and what it does not:

  * It is a test, not a proof. Zero counterexamples means "no difference found in N generated cases".
    So a label never moves from `assumes`/`nulls`/`changes` to `same` because of it.
  * A counterexample against a `same` rewrite is a Gulliver bug: that change is dropped and the result warns.
  * It runs in DuckDB semantics. SQL from another dialect is translated first; when the translator warns,
    verification is skipped instead of testing something else.
  * Column types come from the connection's metadata when known. Otherwise they are inferred from how the
    query uses each column, and every type that fits is tried (the counterexample says which one failed).

The database never sees user data and cannot read files (external access is disabled and locked), so no
budget gate applies: it costs nothing but a little local CPU.
"""

from __future__ import annotations

import random
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

from . import fixer
from .diffing import translate
from .sqlsafety import base_tables, classify, table_ref

DEFAULT_CASES = 200
QUICK_CASES = 40            # preflight's autofix check: a few cases, a short time budget
SIZES = (0, 1, 2, 3, 5, 8, 13, 50)
ZONES = ("UTC", "Europe/Amsterdam", "Atlantic/Azores", "America/Santiago", "Asia/Kolkata")
QUERY_TIMEOUT_S = 5.0
SEMANTICS = "DuckDB semantics"
NOT_TESTED = {
    "GV002": "a LIMIT changes the result on purpose; there is nothing to test",
    "GV007": "an approximate count is exact on tiny tables, so a test would prove nothing",
}
CANDIDATES = {
    "temporal": ["TIMESTAMP", "DATE", "TIMESTAMPTZ", "VARCHAR"],
    "datestr": ["DATE", "TIMESTAMP", "TIMESTAMPTZ", "VARCHAR"],
    "number": ["INTEGER", "DOUBLE"],
    "string": ["VARCHAR"],
    "bool": ["BOOLEAN"],
    "default": ["INTEGER", "VARCHAR"],
}
PRIORITY = {"temporal": 5, "datestr": 4, "number": 3, "string": 2, "bool": 1, "default": 0}
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DEFAULT_ANCHORS = (date(2024, 1, 1), date(2024, 2, 29), date(2024, 10, 27), date(2024, 12, 31))


def _classes(*names: str) -> tuple[type, ...]:
    return tuple(getattr(exp, n) for n in names if hasattr(exp, n))


TEMPORAL_FUNCS = _classes("Date", "TsOrDsToDate", "DateTrunc", "TimestampTrunc", "DatetimeTrunc", "Year",
                          "Quarter", "Month", "Week", "Day", "DayOfWeek", "DayOfYear", "DateAdd", "DateSub",
                          "DateDiff", "Extract", "TimeToStr", "LastDay")
STRING_FUNCS = _classes("Like", "ILike", "Lower", "Upper", "Length", "Substring", "Concat", "DPipe", "Trim",
                        "RegexpLike", "StartsWith")
NUMBER_FUNCS = _classes("Sum", "Avg", "Add", "Sub", "Mul", "Div", "Mod", "Neg", "Abs", "Round", "Stddev")
COMPARISONS = _classes("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "NullSafeEQ", "NullSafeNEQ")
RANDOM_FUNCS = _classes("Rand", "Randn", "Uuid", "TableSample")
WINDOW_ORDERED = _classes("RowNumber", "FirstValue", "LastValue", "NthValue", "Lag", "Lead", "Ntile")
AGG_ORDERED = _classes("GroupConcat", "ArrayAgg", "AnyValue", "First", "Last", "ArgMax", "ArgMin")
RANDOM_NAMES = {"random", "rand", "uuid", "gen_random_uuid", "setseed", "nextval", "randn", "newid"}


class Unsupported(Exception):
    """The query cannot be tested on generated data (the reason is the message)."""


class _BadCase(Exception):
    """The generated data did not fit (an overflow, a timeout): not a counterexample, just a lost case."""


# -- schema ---------------------------------------------------------------------------------
@dataclass
class Column:
    name: str
    types: list[str]                     # DuckDB types to try; one item when the metadata knows it
    known: bool = False
    hint: str = "default"
    literals: list[str] = field(default_factory=list)


@dataclass
class Table:
    ref: str                             # the name the query uses
    name: str                            # gv_tN in the private database
    columns: dict[str, Column] = field(default_factory=dict)


def _duck_type(raw: str, dialect: str) -> str | None:
    try:
        return exp.DataType.build(raw, dialect=dialect).sql("duckdb")
    except Exception:  # an engine type sqlglot does not know: infer it like an unknown column
        return None


def _family(ctype: str) -> str:
    t = ctype.upper()
    if t in ("TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE"):
        return "timestamptz"
    if t.startswith(("TIMESTAMP", "DATETIME")):
        return "timestamp"
    if t == "DATE":
        return "date"
    if t.startswith("TIME"):
        return "time"
    if t.startswith(("DECIMAL", "NUMERIC")):
        return "decimal"
    if t in ("FLOAT", "DOUBLE", "REAL"):
        return "float"
    if t in ("TINYINT", "SMALLINT", "INT", "INTEGER", "BIGINT", "HUGEINT"):
        return "int"
    if t in ("UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"):
        return "uint"
    if t.startswith(("VARCHAR", "TEXT", "STRING", "CHAR")):
        return "text"
    if t in ("BOOLEAN", "BOOL"):
        return "bool"
    if t == "UUID":
        return "uuid"
    return "other"


def _unwrap(node: exp.Expression | None) -> exp.Expression | None:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _versus(other: exp.Expression | None) -> tuple[str, Any]:
    """What a column is compared with tells its type: (hint, literal value or linked column)."""
    other = _unwrap(other)
    if isinstance(other, exp.Column):
        return "link", other
    if isinstance(other, exp.Cast) and isinstance(other.this, exp.Literal):
        to = other.args.get("to")
        if isinstance(to, exp.DataType) and to.is_type(*exp.DataType.TEMPORAL_TYPES):
            return "datestr", other.this.name
        other = other.this
    if isinstance(other, exp.Neg) and isinstance(other.this, exp.Literal) and not other.this.is_string:
        return "number", "-" + other.this.name
    if isinstance(other, exp.Literal):
        if not other.is_string:
            return "number", other.name
        return ("datestr" if _ISO.match(other.name) else "string"), other.name
    if isinstance(other, exp.Boolean):
        return "bool", None
    if isinstance(other, (*TEMPORAL_FUNCS, *_classes("CurrentDate", "CurrentTimestamp"))):
        return "temporal", None
    return "default", None


def _context(col: exp.Column) -> tuple[str, Any]:
    node, parent = col, col.parent
    while isinstance(parent, exp.Paren):
        node, parent = parent, parent.parent
    if isinstance(parent, exp.Cast):
        to = parent.args.get("to")
        temporal = isinstance(to, exp.DataType) and to.is_type(*exp.DataType.TEMPORAL_TYPES)
        return ("temporal" if temporal else "default"), None
    if isinstance(parent, TEMPORAL_FUNCS):
        return "temporal", None
    if isinstance(parent, COMPARISONS):
        return _versus(parent.expression if parent.this is node else parent.this)
    if isinstance(parent, exp.Between):
        return _versus(parent.args.get("low") if parent.this is node else parent.this)
    if isinstance(parent, exp.In) and parent.this is node:
        query = parent.args.get("query")
        sub = query.this if isinstance(query, exp.Subquery) else query
        if isinstance(sub, exp.Select) and sub.expressions:
            return _versus(sub.expressions[0].unalias())
        if parent.expressions:
            return _versus(parent.expressions[0])
    if isinstance(parent, STRING_FUNCS):
        return "string", None
    if isinstance(parent, NUMBER_FUNCS):
        return "number", None
    if isinstance(parent, (exp.Where, exp.Having, exp.And, exp.Or, exp.Not)):
        return "bool", None
    return "default", None


def _select_sources(select: exp.Select, gv: set[str]) -> list[tuple[str, str | None]]:
    """(alias, gv table or None for a derived source) for the FROM and JOIN items of one SELECT."""
    items = []
    frm = select.args.get("from_") or select.args.get("from")
    if frm is not None:
        items.append(frm.this)
    items.extend(j.this for j in select.args.get("joins") or [])
    out = []
    for item in items:
        name = item.name.lower() if isinstance(item, exp.Table) else ""
        out.append(((item.alias_or_name or "").lower(), name if name in gv else None))
    return out


def _owner(col: exp.Column, gv: set[str]) -> str | None:
    """The generated table a column reference belongs to, or None (a CTE, subquery or output alias)."""
    select = col.find_ancestor(exp.Select)
    if col.table:
        key = col.table.lower()
        while select is not None:
            for alias, table in _select_sources(select, gv):
                if alias == key:
                    return table
            select = select.find_ancestor(exp.Select)
        return None
    if select is None:
        return None
    sources = _select_sources(select, gv)
    if not sources or any(t is None for _, t in sources):
        return None
    in_projection = any(c is col for p in select.expressions for c in p.find_all(exp.Column))
    aliases = {p.alias.lower() for p in select.expressions if isinstance(p, exp.Alias)}
    if not in_projection and col.name.lower() in aliases:
        return None
    return sources[0][1]


class Schema:
    def __init__(self, tables: dict[str, Table]):
        self.tables = tables
        self.parent: dict[tuple[str, str], tuple[str, str]] = {}

    def column(self, table: str, name: str) -> Column:
        cols = self.tables[table].columns
        if name not in cols:
            cols[name] = Column(name, list(CANDIDATES["default"]))
        return cols[name]

    def find(self, key: tuple[str, str]) -> tuple[str, str]:
        while self.parent.get(key, key) != key:
            key = self.parent[key]
        return key

    def link(self, a: tuple[str, str], b: tuple[str, str]) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> dict[tuple[str, str], list[tuple[str, str]]]:
        out: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for t in sorted(self.tables):
            for c in self.tables[t].columns:
                out.setdefault(self.find((t, c)), []).append((t, c))
        return out


def _learn(schema: Schema, tree: exp.Expression) -> None:
    gv = set(schema.tables)
    links: list[tuple[exp.Column, exp.Column]] = []
    for col in tree.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            continue
        owner = _owner(col, gv)
        if owner is None:
            continue
        spec = schema.column(owner, col.name.lower())
        hint, extra = _context(col)
        if hint == "link":
            links.append((col, extra))
            continue
        if extra is not None and extra not in spec.literals:
            spec.literals.append(str(extra))
        if not spec.known and PRIORITY[hint] > PRIORITY[spec.hint]:
            spec.hint, spec.types = hint, list(CANDIDATES[hint])
    for join in tree.find_all(exp.Join):
        select = join.find_ancestor(exp.Select)
        for ident in join.args.get("using") or []:
            for _, table in _select_sources(select, gv) if select is not None else []:
                if table:
                    schema.column(table, ident.name.lower())
    for setop in tree.find_all(exp.Union, exp.Except, exp.Intersect):
        left, right = setop.left, setop.right
        for a, b in zip(left.selects, right.selects, strict=False):
            if isinstance(a.unalias(), exp.Column) and isinstance(b.unalias(), exp.Column):
                links.append((a.unalias(), b.unalias()))
    for a, b in links:
        oa, ob = _owner(a, gv), _owner(b, gv)
        if oa and ob:
            schema.column(oa, a.name.lower())
            schema.column(ob, b.name.lower())
            schema.link((oa, a.name.lower()), (ob, b.name.lower()))


def _group_types(schema: Schema) -> dict[tuple[str, str], list[str]]:
    """Per group of linked columns: the types to try (from the strongest hint; metadata wins)."""
    out = {}
    for root, members in schema.groups().items():
        cols = [schema.tables[t].columns[c] for t, c in members]
        if any(c.known for c in cols):
            out[root] = []  # types are fixed per column
            continue
        best = max(cols, key=lambda c: PRIORITY[c.hint])
        out[root] = list(best.types)
    return out


# -- query preparation ------------------------------------------------------------------------
def _check_deterministic(tree: exp.Expression) -> None:
    for node in tree.walk():
        if isinstance(node, RANDOM_FUNCS) or (isinstance(node, exp.Anonymous)
                                              and str(node.name).lower() in RANDOM_NAMES):
            raise Unsupported("the query uses a random or sampling function")
        if isinstance(node, exp.Window) and isinstance(node.this, WINDOW_ORDERED):
            raise Unsupported(f"{node.this.key.upper()} OVER (...) depends on the order of tied rows")
        if isinstance(node, AGG_ORDERED) and node.find(exp.Order) is None:
            raise Unsupported(f"{node.key.upper()} depends on row order, so results could differ by chance")
        if isinstance(node, exp.Distinct) and node.args.get("on") is not None:
            raise Unsupported("DISTINCT ON keeps an arbitrary row per group")
        if isinstance(node, (exp.Limit, exp.Fetch, exp.Offset)) and node.parent is not tree:
            raise Unsupported("a LIMIT inside a subquery keeps arbitrary rows")


def _strip_outer_limit(tree: exp.Expression) -> bool:
    found = False
    for key in ("limit", "offset", "fetch"):
        if tree.args.get(key) is not None:
            tree.set(key, None)
            found = True
    return found


def _prepare(sql: str, dialect: str, names: dict[str, str]) -> tuple[str, bool]:
    """The read part of `sql` in DuckDB SQL, with every base table renamed to its gv_tN table."""
    statement = classify(sql, dialect)
    if statement.kind == "block" or statement.select is None:
        raise Unsupported(statement.reason or "not a single SELECT")
    tree = statement.select.copy()
    for table in base_tables(tree, dialect):
        if not isinstance(table.this, exp.Identifier):
            raise Unsupported("the query reads a table function or a file")
        ref = table_ref(table, dialect).lower()
        gv = names.setdefault(ref, f"gv_t{len(names)}")
        if not table.alias:
            table.set("alias", exp.TableAlias(this=exp.to_identifier(table.name)))
        table.set("this", exp.to_identifier(gv))
        table.set("db", None)
        table.set("catalog", None)
    for col in tree.find_all(exp.Column):
        if col.args.get("db") is not None or col.args.get("catalog") is not None:
            col.set("db", None)
            col.set("catalog", None)
    _check_deterministic(tree)
    stripped = _strip_outer_limit(tree)
    if dialect == "duckdb":
        return tree.sql(dialect="duckdb"), stripped
    out = translate(tree.sql(dialect=dialect), dialect, "duckdb")
    if out["warnings"]:
        raise Unsupported(f"translating from {dialect} to DuckDB warned ({out['warnings'][0]}); testing the "
                          "translation would test something else")
    return out["sql"], stripped


# -- value pools ------------------------------------------------------------------------------
def _anchors(trees: list[exp.Expression]) -> list[date]:
    found: set[date] = set()
    for tree in trees:
        for lit in tree.find_all(exp.Literal):
            m = _ISO.match(lit.name) if lit.is_string else None
            if m:
                try:
                    found.add(date(int(m[1]), int(m[2]), int(m[3])))
                except ValueError:
                    pass
            elif not lit.is_string and lit.name.isdigit() and 1900 <= int(lit.name) <= 2200:
                found.add(date(int(lit.name), 1, 1))
    return sorted(found) or list(_DEFAULT_ANCHORS)


def _days(anchors: list[date]) -> list[date]:
    out: set[date] = {date(2024, 2, 29)}
    for a in anchors:
        month = a.replace(day=1)
        for d in (a, month, date(a.year, 1, 1)):
            for step in (-1, 0, 1):
                try:
                    out.add(d + timedelta(days=step))
                except OverflowError:
                    pass
        try:
            out.add(date(a.year + (a.month == 12), a.month % 12 + 1, 1))
            out.add(date(a.year + 1, 1, 1))
        except ValueError:
            pass
    return sorted(out)


class _Pools:
    def __init__(self, anchors: list[date]):
        self.anchors = anchors
        self.days = _days(anchors)
        self.zoned: dict[str, list[str]] = {}

    def stamps(self) -> list[str]:
        out = []
        for d in self.days:
            out += [f"{d} 00:00:00", f"{d} 00:00:00.000001", f"{d} 12:00:00", f"{d} 23:59:59.999999"]
        return out + ["infinity", "-infinity"]

    def zoned_stamps(self, con: duckdb.DuckDBPyConnection, zone: str) -> list[str]:
        """Instants around local midnights and the zone's daylight-saving jumps, as text with offsets."""
        if zone not in self.zoned:
            days = ", ".join(f"(DATE '{d}')" for d in self.days)
            years = sorted({d.year for d in self.anchors})
            jumps = " UNION ALL ".join(
                f"SELECT h FROM (SELECT h, date_part('timezone', h) AS o, lag(date_part('timezone', h)) OVER "
                f"(ORDER BY h) AS p FROM range(TIMESTAMPTZ '{y}-01-01 00:00:00+00', TIMESTAMPTZ "
                f"'{y + 1}-01-01 00:00:00+00', INTERVAL '1 hour') r(h)) WHERE o <> p" for y in years if y < 9999)
            sql = (f"SELECT DISTINCT (m + s)::VARCHAR FROM (SELECT CAST(d AS TIMESTAMPTZ) AS m FROM (VALUES {days}) "
                   f"v(d) {'UNION ALL ' + jumps if jumps else ''}) x, (VALUES (INTERVAL '-1 hour'), "
                   "(INTERVAL '-30 minutes'), (INTERVAL '-1 microsecond'), (INTERVAL '0 seconds'), "
                   "(INTERVAL '1 microsecond'), (INTERVAL '30 minutes')) y(s)")
            self.zoned[zone] = sorted(r[0] for r in con.execute(sql).fetchall()) + ["infinity", "-infinity"]
        return self.zoned[zone]

    def pool(self, col: Column, ctype: str, key: bool, con: duckdb.DuckDBPyConnection,
             zone: str) -> list[str | None]:
        family = _family(ctype)
        temporal_text = col.hint in ("temporal", "datestr")
        lits = list(col.literals)
        if family in ("int", "uint"):
            base = ["1", "2", "3"] if key else ["0", "1", "-1", "2", "7", "100", "2147483647", "-2147483648"]
            for lit in lits:
                if re.fullmatch(r"-?\d+", lit):
                    base += [str(int(lit) + step) for step in (-1, 0, 1)]
            if family == "uint":
                base = [v for v in base if not v.startswith("-")]
            return [None, *base]
        if family == "float":
            base = ["1", "2", "3"] if key else ["0", "-0.0", "0.5", "1", "-1.25", "2", "nan", "inf", "-inf"]
            return [None, *base, *(x for x in lits if re.fullmatch(r"-?[\d.]+(e-?\d+)?", x))]
        if family == "decimal":
            return [None, "0", "1", "-1", "2.5", *(x for x in lits if re.fullmatch(r"-?[\d.]+", x))]
        if family == "text":
            if temporal_text:
                out: list[str | None] = [None]
                for d in self.days:
                    out += [str(d), f"{d} 00:00:00", f"{d} 23:59:59", f"{d}T12:00:00", f"{d.year}-{d.month}-{d.day}"]
                return out + lits
            base = ["a", "b", ""] if key else ["", "a", "A", "a ", " a", "b", "é", "2024-01-01"]
            return [None, *base, *lits]
        if family == "bool":
            return [None, "true", "false"]
        if family == "date":
            return [None, *(str(d) for d in self.days), "infinity", "-infinity"]
        if family == "timestamp":
            return [None, *self.stamps()]
        if family == "timestamptz":
            return [None, *self.zoned_stamps(con, zone)]
        if family == "time":
            return [None, "00:00:00", "12:00:00", "23:59:59.999999"]
        if family == "uuid":
            return [None, "00000000-0000-0000-0000-000000000000", "ffffffff-ffff-ffff-ffff-ffffffffffff"]
        return [None]


# -- running ----------------------------------------------------------------------------------
def _literal(value: str | None, ctype: str = "") -> str:
    if value is None:
        return "NULL"
    if _family(ctype) in ("int", "uint", "float", "decimal") and re.fullmatch(r"-?\d+(\.\d+)?", value):
        return value
    return "'" + value.replace("'", "''") + "'"


def _ident(name: str) -> str:
    return exp.to_identifier(name, quoted=True).sql(dialect="duckdb")


@dataclass
class Case:
    types: dict[tuple[str, str], str]               # (table, column) -> DuckDB type
    rows: dict[str, list[list[str | None]]]         # table -> rows in column order
    zone: str
    signature: dict[str, str] = field(default_factory=dict)


class Runner:
    def __init__(self, schema: Schema, deadline: float):
        self.schema = schema
        self.deadline = deadline              # no new cases or shrink steps after this
        self.hard_deadline = deadline + 5.0   # room to finish the counterexample in hand
        self._cons: dict[str, duckdb.DuckDBPyConnection] = {}

    def con(self, zone: str) -> duckdb.DuckDBPyConnection:
        if zone not in self._cons:
            con = duckdb.connect(":memory:")
            con.execute("SET threads = 1")
            con.execute(f"SET TimeZone = {_literal(zone)}")
            con.execute("SET enable_external_access = false")
            con.execute("SET lock_configuration = true")
            self._cons[zone] = con
        return self._cons[zone]

    def close(self) -> None:
        for con in self._cons.values():
            con.close()

    def _query(self, con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple]:
        left = self.hard_deadline - time.monotonic()
        if left <= 0:
            raise _BadCase("time budget used up")
        timer = threading.Timer(min(QUERY_TIMEOUT_S, left), con.interrupt)
        timer.start()
        try:
            return con.execute(sql).fetchall()
        except duckdb.InterruptException as exc:
            raise _BadCase("a query took too long on the generated data") from exc
        finally:
            timer.cancel()

    def run(self, case: Case, sqls: list[str]) -> list[tuple[str, Any]]:
        """("rows", (column count, Counter of rows as text)) or ("error", message) per query."""
        con = self.con(case.zone)
        con.execute("BEGIN")  # one transaction: now() and current_date are the same for every query
        try:
            for t in sorted(self.schema.tables):
                table = self.schema.tables[t]
                cols = ", ".join(f"{_ident(c)} {case.types[(t, c)]}" for c in table.columns)
                rows = case.rows.get(t) or []
                values = ", ".join("(" + ", ".join(_literal(v) for v in row) + ")" for row in rows)
                try:
                    con.execute(f"CREATE TABLE {t} ({cols})")
                    if rows:
                        con.execute(f"INSERT INTO {t} VALUES {values}")
                except duckdb.Error as exc:
                    raise _BadCase(str(exc).splitlines()[0]) from exc
            out: list[tuple[str, Any]] = []
            for sql in sqls:
                try:
                    rows = self._query(con, f"SELECT COLUMNS(*)::VARCHAR FROM ({sql}) AS gv_result")
                    width = len(con.description or [])
                    out.append(("rows", (width, Counter(rows))))
                except duckdb.Error as exc:
                    out.append(("error", str(exc).splitlines()[0]))
            return out
        finally:
            con.execute("ROLLBACK")

    def binds(self, case: Case, sql: str) -> str | None:
        """None when `sql` binds on these (empty) tables, else the engine's message."""
        try:
            kind, value = self.run(case, [sql])[0]
        except _BadCase as exc:
            return str(exc)
        return value if kind == "error" else None


def _differs(a: tuple[str, Any], b: tuple[str, Any]) -> bool:
    if a[0] == "error" and b[0] == "error":
        return False
    return a != b


# -- the check --------------------------------------------------------------------------------
def _build_schema(names: dict[str, str], trees: list[exp.Expression], table_info: dict[str, dict] | None,
                  dialect: str) -> tuple[Schema, str]:
    tables = {gv: Table(ref.replace("`", "").replace('"', ""), gv) for ref, gv in names.items()}
    used_meta = False
    for ref, gv in names.items():
        info = (table_info or {}).get(ref) or {}
        for c in info.get("columns") or []:
            ctype = _duck_type(str(c.get("type", "")), dialect)
            if ctype:
                name = str(c.get("name", "")).lower()
                tables[gv].columns[name] = Column(name, [ctype], known=True)
                used_meta = True
    schema = Schema(tables)
    inferred = False
    for tree in trees:
        before = sum(len(t.columns) for t in tables.values())
        _learn(schema, tree)
        inferred = inferred or sum(len(t.columns) for t in tables.values()) > before
    for table in tables.values():
        if not table.columns:
            table.columns["gv_rowid"] = Column("gv_rowid", ["INTEGER"])
        inferred = inferred or any(not c.known for c in table.columns.values())
    source = "metadata" if used_meta and not inferred else ("metadata+inferred" if used_meta else "inferred")
    return schema, source


def _primary(schema: Schema, groups: dict[tuple[str, str], list[str]], choice: dict) -> dict[tuple[str, str], str]:
    types = {}
    for root, members in schema.groups().items():
        cols = [schema.tables[t].columns[c] for t, c in members]
        known = next((c.types[0] for c in cols if c.known), None)  # a linked unknown column takes its type
        for (t, c), col in zip(members, cols, strict=True):
            if col.known:
                types[(t, c)] = col.types[0]
            else:
                types[(t, c)] = known or choice.get(root) or (groups.get(root) or col.types)[0]
    return types


def _fit_types(runner: Runner, schema: Schema, groups: dict, original: str) -> dict[tuple[str, str], list[str]]:
    """Find types the original binds to; per uncertain group keep every candidate that binds."""
    empty = {t: [] for t in schema.tables}
    choice: dict = {}
    error = runner.binds(Case(_primary(schema, groups, choice), empty, "UTC"), original)
    for _ in range(10):  # a column the inference missed: add it where the engine says it is missing
        missing = re.search(r'Referenced column "([^"]+)" not found|does not have a column named "([^"]+)"',
                            error or "")
        if not missing:
            break
        name = (missing[1] or missing[2]).lower()
        target = next((t for t in sorted(schema.tables) if name not in schema.tables[t].columns), None)
        if target is None:
            break
        schema.column(target, name)
        groups = _group_types(schema)
        error = runner.binds(Case(_primary(schema, groups, choice), empty, "UTC"), original)
    if error:
        for root in sorted(groups):  # greedy: change one group at a time until it binds
            for alt in groups[root][1:]:
                trial = {**choice, root: alt}
                if runner.binds(Case(_primary(schema, groups, trial), empty, "UTC"), original) is None:
                    choice, error = trial, None
                    break
            if error is None:
                break
    if error:
        raise Unsupported(f"could not build tables the query binds to ({error})")
    valid = {}
    for root, cands in groups.items():
        if not cands:
            valid[root] = []
            continue
        primary = choice.get(root, cands[0])
        hint = max((schema.tables[t].columns[c].hint for t, c in schema.groups()[root]), key=PRIORITY.get)
        if hint not in ("temporal", "datestr"):
            valid[root] = [primary]  # only date-like columns are worth trying in several types
            continue
        ok = [c for c in cands if c != primary and runner.binds(
            Case(_primary(schema, groups, {**choice, root: c}), empty, "UTC"), original) is None]
        valid[root] = [primary, *ok]
    return valid


def _draw(rng: random.Random, schema: Schema, valid: dict, pools: _Pools, runner: Runner, index: int,
          timezone: str | None) -> Case:
    choice = {root: rng.choice(cands) for root, cands in sorted(valid.items()) if cands}
    types = _primary(schema, valid, choice)
    zoned = any(_family(t) == "timestamptz" for t in types.values())
    zone = timezone or (rng.choice(ZONES) if zoned else "UTC")
    con = runner.con(zone)
    signature = {}
    for root, cands in sorted(valid.items()):
        if len(cands) > 1:
            signature[f"{schema.tables[root[0]].ref}.{root[1]}"] = choice[root]
    if zoned and not timezone:
        signature["timezone"] = zone
    groups = schema.groups()
    keyed = {m for members in groups.values() if len(members) > 1 for m in members}
    rows: dict[str, list[list[str | None]]] = {}
    for t in sorted(schema.tables):
        cols = list(schema.tables[t].columns)
        col_pools = [pools.pool(schema.tables[t].columns[c], types[(t, c)], (t, c) in keyed, con, zone)
                     for c in cols]
        n = 0 if index == 0 else 1 if index == 1 else rng.choice(SIZES)
        out: list[list[str | None]] = []
        for _ in range(n):
            if out and rng.random() < 0.15:
                out.append(list(rng.choice(out)))  # a duplicate row
                continue
            out.append([rng.choice(out)[i] if out and rng.random() < 0.2 else rng.choice(p)
                        for i, p in enumerate(col_pools)])
        rows[t] = out
    return Case(types, rows, zone, signature)


def _shrink(runner: Runner, case: Case, original: str, variant: str) -> Case:
    """Delete rows (in chunks, then one by one) while the difference stays."""
    def fails(rows: dict[str, list]) -> bool:
        try:
            a, b = runner.run(Case(case.types, rows, case.zone), [original, variant])
        except _BadCase:
            return False
        return _differs(a, b)

    rows = {t: list(r) for t, r in case.rows.items()}
    for _ in range(3):
        changed = False
        for t in sorted(rows):
            chunk = max(1, len(rows[t]) // 2)
            while rows[t]:
                i, progress = 0, False
                while i < len(rows[t]):
                    trial = {**rows, t: rows[t][:i] + rows[t][i + chunk:]}
                    if time.monotonic() < runner.deadline and fails(trial):
                        rows, progress, changed = trial, True, True
                    else:
                        i += chunk
                if chunk == 1 and not progress:
                    break
                if not progress:
                    chunk = max(1, chunk // 2)
        if not changed:
            break
    return Case(case.types, rows, case.zone, case.signature)


def _show(result: tuple[str, Any], limit: int = 20) -> Any:
    if result[0] == "error":
        return {"error": result[1]}
    return [list(r) for r in sorted(result[1][1].elements(), key=lambda r: [(v is None, v or "") for v in r])][:limit]


def _counterexample(runner: Runner, schema: Schema, case: Case, original: str, variant: str) -> dict[str, Any]:
    a, b = runner.run(case, [original, variant])
    tables, setup = {}, []
    for t in sorted(schema.tables):
        table = schema.tables[t]
        cols = list(table.columns)
        tables[table.ref] = {"columns": {c: case.types[(t, c)] for c in cols},
                             "rows": [dict(zip(cols, r, strict=True)) for r in case.rows.get(t) or []]}
        setup.append(f"CREATE TABLE {table.ref} (" + ", ".join(f"{_ident(c)} {case.types[(t, c)]}" for c in cols)
                     + ");")
        if case.rows.get(t):
            setup.append(f"INSERT INTO {table.ref} VALUES " + ", ".join(
                "(" + ", ".join(_literal(v, case.types[(t, c)]) for c, v in zip(cols, r, strict=True)) + ")"
                for r in case.rows[t]) + ";")
    if any(_family(v) == "timestamptz" for v in case.types.values()):
        setup.insert(0, f"SET TimeZone = {_literal(case.zone)};")
    out: dict[str, Any] = {"tables": tables, "original_result": _show(a), "fixed_result": _show(b),
                           "setup_sql": "\n".join(setup)}
    if a[0] == b[0] == "rows":
        out["only_in_original"] = _show(("rows", (0, a[1][1] - b[1][1])))
        out["only_in_fixed"] = _show(("rows", (0, b[1][1] - a[1][1])))
        if a[1][0] != b[1][0]:
            out["column_count"] = {"original": a[1][0], "fixed": b[1][0]}
    if case.signature:
        out["types"] = case.signature
    return out


def check(original: str, candidates: list[str], dialect: str = "duckdb", table_info: dict[str, dict] | None = None,
          cases: int = DEFAULT_CASES, seed: int = 0, timezone: str | None = None,
          time_budget_s: float = 30.0) -> dict[str, Any]:
    """Run `original` and every candidate rewrite on the same generated tables and compare the results.

    Returns {"status", "cases", "results": [{cases, counterexamples, by_type?, counterexample?} per candidate],
    ...}. Status "skipped" (with "reason") when the query cannot be tested fairly."""
    started = time.monotonic()
    cases = max(1, int(cases))
    base: dict[str, Any] = {"engine": "duckdb", "semantics": SEMANTICS, "seed": seed}
    names: dict[str, str] = {}
    try:
        original_sql, stripped = _prepare(original, dialect, names)
        variant_sqls = [_prepare(c, dialect, names)[0] for c in candidates]
    except Unsupported as exc:
        return {**base, "status": "skipped", "reason": str(exc), "results": []}
    except sqlglot.errors.SqlglotError as exc:
        return {**base, "status": "skipped", "reason": f"could not parse: {str(exc).splitlines()[0]}", "results": []}
    trees = [sqlglot.parse_one(s, read="duckdb") for s in [original_sql, *variant_sqls]]
    schema, source = _build_schema(names, trees, table_info, dialect)
    runner = Runner(schema, started + time_budget_s)
    try:
        try:
            valid = _fit_types(runner, schema, _group_types(schema), original_sql)
        except (Unsupported, _BadCase) as exc:
            return {**base, "status": "skipped", "reason": str(exc), "results": []}
        pools = _Pools(_anchors(trees))
        zone = timezone or ("UTC" if dialect == "bigquery" else None)  # BigQuery has no session time zone
        rng = random.Random(seed)  # noqa: S311 - test data, not secrets
        stats = [{"cases": 0, "counterexamples": 0, "by_type": {}} for _ in candidates]
        first: list[Case | None] = [None] * len(candidates)
        ran = invalid = both_failed = 0
        stopped = False
        for index in range(cases):
            if time.monotonic() >= runner.deadline:
                stopped = True
                break
            case = _draw(rng, schema, valid, pools, runner, index, zone)
            try:
                results = runner.run(case, [original_sql, *variant_sqls])
            except _BadCase:
                invalid += 1
                continue
            ran += 1
            both_failed += results[0][0] == "error"
            sig = tuple(sorted(case.signature.items()))
            for i, result in enumerate(results[1:]):
                bucket = stats[i]["by_type"].setdefault(sig, {"types": dict(sig), "cases": 0, "counterexamples": 0})
                stats[i]["cases"] += 1
                bucket["cases"] += 1
                if _differs(results[0], result):
                    stats[i]["counterexamples"] += 1
                    bucket["counterexamples"] += 1
                    if first[i] is None:
                        first[i] = case
        out_results = []
        for i, stat in enumerate(stats):
            item: dict[str, Any] = {"cases": stat["cases"], "counterexamples": stat["counterexamples"]}
            if any(stat["by_type"]):  # some types or the time zone varied between cases
                item["by_type"] = sorted(stat["by_type"].values(), key=lambda b: sorted(b["types"].items()))
            if first[i] is not None:
                small = _shrink(runner, first[i], original_sql, variant_sqls[i])
                item["counterexample"] = _counterexample(runner, schema, small, original_sql, variant_sqls[i])
            out_results.append(item)
    finally:
        runner.close()
    notes = []
    if stripped:
        notes.append("Compared the full results without the outer LIMIT/OFFSET (it only cuts the same rows).")
    if dialect != "duckdb":
        notes.append(f"Translated from {dialect} to DuckDB for the test; the target engine may differ in details.")
    if both_failed:
        notes.append(f"In {both_failed} case(s) the original query failed on the generated data (both failing "
                     "counts as agreeing).")
    if stopped:
        notes.append(f"Stopped after {ran} cases: the time budget of {time_budget_s:g} s was used up.")
    status = "warn" if any(r["counterexamples"] for r in out_results) else "ok"
    if ran == 0 or ran == both_failed:
        status = "skipped"
        base["reason"] = "no generated case ran the original query successfully"
    schema_out = {}
    for t in sorted(schema.tables):
        table = schema.tables[t]
        schema_out[table.ref] = {}
        for c, col in table.columns.items():
            root = schema.find((t, c))
            schema_out[table.ref][c] = col.types[0] if col.known else " | ".join(valid.get(root) or col.types)
    return {**base, "status": status, "cases": ran, "invalid_cases": invalid, "stopped_early": stopped,
            "elapsed_s": round(time.monotonic() - started, 3), "schema": schema_out, "schema_source": source,
            "notes": notes, "results": out_results}


# -- gulliver_fix integration -----------------------------------------------------------------
def verify_rewrite(sql: str, dialect: str = "duckdb", mode: str = "safe", limit: int = 1000,
                   table_info: dict[str, dict] | None = None, cases: int = DEFAULT_CASES, seed: int = 0,
                   timezone: str | None = None, time_budget_s: float = 30.0) -> dict[str, Any]:
    """fixer.rewrite(), with every change tested on generated data. A `same` change with a counterexample
    is dropped (listed in `dropped`); other labels never change, they only gain the test result."""
    result, offered = fixer._rewrite(sql, dialect, mode, limit, table_info)
    if result.get("status") == "block" or not offered:
        if result.get("status") != "block":
            result["verify"] = {"status": "skipped", "reason": "nothing was rewritten, so there is nothing to test"}
        return result
    n = len(offered)
    tested = [i for i, c in enumerate(offered) if c.rule not in NOT_TESTED]
    variants = []
    for i in tested:
        alone, again = fixer._rewrite(sql, dialect, mode, limit, table_info, skip=frozenset(range(n)) - {i})
        if len(again) != n:
            return {**result, "verify": {"status": "error", "error": "the rewrite was not repeatable"}}
        variants.append(alone["fixed_sql"])
    report: dict[str, Any] = {"status": "skipped", "results": [], "reason": "no change can be tested"}
    if tested:
        try:
            report = check(sql, variants, dialect, table_info, cases=cases, seed=seed, timezone=timezone,
                           time_budget_s=time_budget_s)
        except Exception as exc:  # a test harness problem must not hide the rewrite itself
            report = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "results": []}
    changes = result["changes"]
    for i, change in enumerate(changes):
        if offered[i].rule in NOT_TESTED:
            change["verified_on_generated_data"] = {"skipped": NOT_TESTED[offered[i].rule]}
    if report["status"] in ("skipped", "error"):
        for i in tested:
            changes[i]["verified_on_generated_data"] = {"skipped": report.get("reason") or report.get("error", "")}
    for pos, i in enumerate(tested):
        if pos >= len(report["results"]):
            break
        item = dict(report["results"][pos])
        example = item.pop("counterexample", None)
        item["meaning"] = (f"no difference found in {item['cases']} generated cases ({SEMANTICS}); a test, not a proof"
                           if not item["counterexamples"] else
                           f"rows differ in {item['counterexamples']} of {item['cases']} generated cases")
        changes[i]["verified_on_generated_data"] = item
        if example is not None:
            changes[i]["counterexample"] = example
    drop = frozenset(i for i in tested if changes[i]["guarantee"] == fixer.SAME
                     and (changes[i].get("verified_on_generated_data") or {}).get("counterexamples"))
    summary = {k: v for k, v in report.items() if k != "results"}
    if drop:
        kept, _ = fixer._rewrite(sql, dialect, mode, limit, table_info, skip=drop)
        for new, old in zip(kept["changes"], [changes[i] for i in range(n) if i not in drop], strict=True):
            new.update({k: old[k] for k in ("verified_on_generated_data", "counterexample") if k in old})
        kept["dropped"] = [changes[i] for i in sorted(drop)]
        result = kept
    result["verify"] = summary
    return result


def finish(result: dict[str, Any]) -> None:
    """Fold the test results into status and next_step (called by fixer.fix after its own decision)."""
    report = result.get("verify")
    if not report:
        return
    if result.get("dropped"):
        result["status"] = "warn"
        rules = ", ".join(sorted({c["rule"] for c in result["dropped"]}))
        result["next_step"] = (f"Gulliver dropped its own {rules} rewrite: it claimed the same rows, but generated "
                               "data found a difference (see dropped[].counterexample). This is a Gulliver bug; "
                               "please report it. " + result.get("next_step", ""))
        return
    differing = [c for c in result.get("changes") or [] if c.get("counterexample")]
    if differing:
        rules = ", ".join(sorted({c["rule"] for c in differing}))
        result["next_step"] = (result.get("next_step", "") + f" Generated data shows rows that differ for {rules} "
                               "(see changes[].counterexample): only use it if that data cannot occur.").strip()
    elif report.get("status") == "ok":
        result["next_step"] = (result.get("next_step", "") + f" Tested on {report['cases']} generated cases "
                               f"({SEMANTICS}): no difference found. That is a test, not a proof.").strip()
    elif report.get("status") in ("skipped", "error") and result.get("changes"):
        why = report.get("reason") or report.get("error") or ""
        result["next_step"] = (result.get("next_step", "") + f" Not tested on generated data: {why}.").strip()


def quick_rewrite(sql: str, dialect: str, table_info: dict[str, dict] | None) -> dict[str, Any]:
    """For preflight's autofix: the safe rewrite, checked on a few generated cases within a short time."""
    out = fixer.rewrite(sql, dialect, mode="safe", table_info=table_info)
    if not out.get("changes"):
        return out
    try:
        return verify_rewrite(sql, dialect, "safe", 1000, table_info, cases=QUICK_CASES, time_budget_s=2.0)
    except Exception as exc:  # the check is a bonus: never lose the proven rewrite because of it
        out["verify"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        return out


def human_lines(change: dict[str, Any]) -> list[str]:
    """CLI lines for one change's test result."""
    v = change.get("verified_on_generated_data")
    if not v:
        return []
    if "skipped" in v:
        return [f"  tested: not tested ({v['skipped']})"]
    lines = [f"  tested: {v['meaning']}"]
    if v.get("by_type"):
        lines.append("  by type: " + "; ".join(
            " ".join(f"{val}" if k != "timezone" else f"in {val}" for k, val in b["types"].items())
            + f" {b['counterexamples']}/{b['cases']}" for b in v["by_type"]))
    example = change.get("counterexample")
    if example:
        if example.get("types"):
            lines.append("  case:   " + ", ".join(f"{k} = {val}" for k, val in example["types"].items()))
        lines.append("  repro:  " + example["setup_sql"].replace("\n", "\n          "))
        lines.append(f"  original returns: {example['original_result']}")
        lines.append(f"  fixed returns:    {example['fixed_result']}")
    return lines
