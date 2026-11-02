"""Rewrite a query so it reads a sample of its tables instead of the full tables.

Every chosen table reference `t AS x` becomes `(SELECT * FROM t <sample clause>) AS x`, so
the rest of the query (joins, filters, column references) stays exactly the same.

A table can appear more than once (a self-join). Each occurrence is its own sample: clause_for gets the
occurrence number so engines can give every occurrence its own seed. Two occurrences sampled with the same
seed would hold the *same* rows, so every row would always meet itself and small samples would look far
bigger than f^2 of the full join. With first_only=True only the first occurrence is sampled and the others
stay complete, so the output scales linearly with the fraction, like a key join against a full table.
"""

from __future__ import annotations

import re

from sqlglot import exp

from .sqlsafety import base_tables, table_ref


def percent_text(fraction: float) -> str:
    pct = fraction * 100
    text = f"{pct:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _split_shared_ctes(work: exp.Expression, dialect: str, only: set[str] | None) -> None:
    """Give every extra use of a CTE that reads a sampled table its own copy of that CTE.

    `WITH e AS (SELECT * FROM events) ... FROM e a JOIN e b` is a self-join too, but `events` appears only once,
    so both sides would read the same sample. With a copy per use, each use is a separate occurrence.
    Recursive CTEs are left alone.
    """
    for _ in range(8):  # copies of a CTE can make an earlier CTE shared; repeat until stable
        changed = False
        sampled_ctes: set[str] = set()  # CTEs that read a sampled table, directly or through another CTE
        for cte in work.find_all(exp.CTE):
            refs = {table_ref(t, dialect).lower() for t in cte.this.find_all(exp.Table)}
            if (only is None and base_tables(cte.this, dialect)) or refs & (only or set()) or refs & sampled_ctes:
                sampled_ctes.add(cte.alias_or_name.lower())
        for with_ in list(work.find_all(exp.With)):
            if with_.args.get("recursive"):
                continue
            ctes = list(with_.expressions)
            out: list[exp.Expression] = []
            for cte in ctes:
                out.append(cte)
                name = cte.alias_or_name.lower()
                if name not in sampled_ctes:
                    continue
                uses = [t for t in work.find_all(exp.Table)
                        if isinstance(t.this, exp.Identifier) and t.name.lower() == name
                        and not t.args.get("db") and not t.args.get("catalog")]
                for n, use in enumerate(uses[1:], start=1):
                    copy_name = f"{name}_gv{n}"
                    twin = cte.copy()
                    twin.set("alias", exp.TableAlias(this=exp.to_identifier(copy_name)))
                    out.append(twin)
                    if not use.alias:
                        use.set("alias", exp.TableAlias(this=exp.to_identifier(use.name)))
                    use.set("this", exp.to_identifier(copy_name))
                    changed = True
            with_.set("expressions", out)
        if not changed:
            return


def sampling_tree(tree: exp.Expression, dialect: str, only: set[str] | None = None) -> exp.Expression:
    """A copy of the query in which every table occurrence that will be sampled is a separate table node."""
    work = tree.copy()
    _split_shared_ctes(work, dialect, only)
    return work


def sample_query(tree: exp.Expression, dialect: str, clause_for, only: set[str] | None = None,
                 first_only: bool = False) -> tuple[str, list[str]]:
    """Return (sql, sampled_table_refs).

    clause_for(ref, occurrence) -> the engine's sample clause for that table, e.g. "TABLESAMPLE 1% (system, 42)".
        occurrence is 0 for the first time a table is sampled in the query, 1 for the second, and so on.
    only: lower-case table refs to sample; None samples every base table.
    first_only: sample only the first occurrence of each table; later occurrences read the full table.
    """
    work = sampling_tree(tree, dialect, only)
    replacements: dict[str, str] = {}
    sampled: list[str] = []
    seen: dict[str, int] = {}
    for index, table in enumerate(base_tables(work, dialect)):
        ref = table_ref(table, dialect)
        key = ref.lower()
        if only is not None and key not in only:
            continue
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        if first_only and occurrence > 0:
            continue
        if isinstance(table.this, exp.Identifier):
            alias = table.alias or table.name
        else:
            alias = table.alias or f"gv_t{index}"
        placeholder = f"gvsampleplaceholder{index}"
        replacement = exp.Table(
            this=exp.to_identifier(placeholder),
            alias=exp.TableAlias(this=exp.to_identifier(alias)),
        )
        table.replace(replacement)
        replacements[placeholder] = f"(SELECT * FROM {ref} {clause_for(ref, occurrence)})"
        if ref not in sampled:
            sampled.append(ref)
    sql = work.sql(dialect=dialect)
    for placeholder, subquery in replacements.items():
        sql = re.sub(rf"[`\"\[]?{placeholder}[`\"\]]?", lambda _m, s=subquery: s, sql)
    return sql, sampled
