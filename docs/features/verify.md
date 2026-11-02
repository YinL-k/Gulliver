# Test every rewrite on generated edge cases: `gulliver fix --verify`

`gulliver fix` labels each rewrite: `same` (proven from the column types), `assumes` (same rows *if* the column
is a DATE or a TIMESTAMP without time zone), `nulls` (NOT IN → NOT EXISTS) or `changes` (on purpose). Labels are
reasoning. `--verify` adds evidence: it runs the original and each rewrite on small generated tables full of the
data that breaks rewrites, and shows you the rows when they disagree.

Rewriters usually ask you to trust them. SQL linters have shipped auto-fixes that changed query results, and
LLM rewriters give no guarantee at all. `--verify` tests each rewrite and gives you a counterexample you can run.

## Commands

```bash
gulliver fix "SQL" --verify                       # 200 cases, seed 0
gulliver fix "SQL" --verify --cases 500 --seed 7  # more cases, other data; the same seed gives the same result
gulliver fix "SQL" --verify --timezone Europe/Amsterdam   # test in your session time zone
gulliver fix "SQL" --mode all --verify --json     # also the opt-in rewrites, machine-readable
```

MCP: `gulliver_fix(sql, verify=true, cases=200, seed=0, timezone=null)`.

`gulliver_preflight` runs a quick check (40 cases, at most about 2 seconds) on the `autofix` it hands over, and
reports it in `autofix.verified`.

## Example

```text
$ gulliver fix --mode all --verify \
    "select o.id from orders o where o.customer_id not in (select customer_id from blocked)"
GV012 [same rows unless a NULL is involved (in the subquery, or in the outer column)]
  before: NOT o.customer_id IN (SELECT customer_id FROM blocked)
  after:  NOT EXISTS(SELECT 1 FROM blocked AS gv_sub WHERE gv_sub.customer_id = o.customer_id)
  tested: rows differ in 112 of 200 generated cases
  repro:  CREATE TABLE "orders" ("id" INTEGER, "customer_id" INTEGER);
          INSERT INTO "orders" VALUES (0, NULL);
          CREATE TABLE "blocked" ("customer_id" INTEGER);
          INSERT INTO "blocked" VALUES (1);
  original returns: []
  fixed returns:    [['0']]
```

The counterexample is shrunk to the fewest rows that still show the difference (here: one order with no
customer). `repro` is DuckDB SQL you can paste and run: for a schema-qualified name such as `analytics.orders`
or `proj.ds.orders` it creates the schema (and attaches an in-memory catalog) first. The JSON also has
`original_sql` and `fixed_sql`, the two queries as they ran, in DuckDB SQL on those tables.

When a column type is unknown, every type that fits the query is tried, and the result is split per type:

```text
GV005 [same rows if the column is a DATE or a TIMESTAMP without time zone]
  tested: rows differ in 11 of 100 generated cases
  by type: DATE 0/25; TIMESTAMP 0/22; TIMESTAMPTZ in Atlantic/Azores 0/5; ...; VARCHAR 11/26
  case:   events.ts = VARCHAR
```

So the rewrite holds for DATE and TIMESTAMP, and breaks when `ts` is text (`'2024-1-30'` is a date, but it
sorts after `'2024-01-31'` as text). Time zones matter too: in Atlantic/Azores the clock goes back from 01:00
to 00:00, so 27 October 2024 starts twice and a day-range literal on a `TIMESTAMPTZ` column misses an hour. A
default run tries several zones, including that one; `--timezone Atlantic/Azores` tests only that zone (it found
the case with each of 20 seeds we tried, 200 cases each).

JSON: each change gets `verified_on_generated_data` (`cases`, `counterexamples`, `meaning`, and `by_type` when
types or time zones varied) and, when rows differ, `counterexample` (`tables` with column types and rows,
`original_result`, `fixed_result`, `only_in_original`, `only_in_fixed`, `setup_sql`, `original_sql`,
`fixed_sql`, `types`). The top-level `verify` block has `status`, `cases` (the cases the original query ran on
without an error), `original_failed` and `weak` (when the original failed on more than half the cases),
`invalid_cases` (cases that could not be built or took too long), `seed`, `schema` (the types used), `schema_source` (`metadata`, `inferred`
or both), `semantics: "DuckDB semantics"` and `notes`.

## What the generated data contains

Each case is 0, 1, 2, 3, 5, 8, 13 or 50 rows per table. Values are seeded draws from edge-case pools: NULL,
duplicate values and duplicate rows, 0, -1, the integer limits (of the column's type when it is narrower than
INTEGER), NaN, ±infinity (dates and timestamps only for DuckDB, and for Postgres when the query does not extract
a date part: BigQuery and Snowflake cannot store infinity, and Postgres's `EXTRACT(YEAR FROM 'infinity')` is
Infinity where DuckDB's is NULL), empty and padded strings, leap
days, month and year boundaries, `00:00:00.000001` and `23:59:59.999999` next to every date the query
mentions, and the instants around daylight-saving jumps in UTC, Europe/Amsterdam, Atlantic/Azores,
America/Santiago and Asia/Kolkata. Join keys, `IN` subquery columns and `UNION` columns come from one small shared
set of values, so joins, anti-joins and duplicates really happen.

Types come from the connection's metadata (`gulliver_inspect`) when Gulliver has it. Otherwise they are inferred
from how the query uses each column (a date function, a comparison with a number or with `'2024-01-01'`, LIKE,
SUM ...). A column read through a CTE or a derived table (`with x as (select ts from events) ... date(ts)`, also
through `select *` and aliases) is followed back to its base table. A column the inference missed is added when
the engine reports it missing.

## Guarantees

- **Labels never move up.** A test is not a proof: an `assumes` or `nulls` change with zero counterexamples stays
  `assumes` or `nulls`, with `"no difference found in N generated cases (DuckDB semantics); a test, not a proof"`.
- **A counterexample against a `same` rewrite drops that change.** That would be a Gulliver bug. The change moves
  to `dropped` (with its counterexample), `fixed_sql` is rebuilt without it, the status becomes `warn` and
  `next_step` says to report it.
- **Deterministic.** The same SQL, types, seed and case count give the same cases and the same result. A run
  stops at a time budget (30 s; 2 s inside preflight) and then says how many cases ran (`stopped_early`). A
  query running at the end of the budget is stopped; only a counterexample in hand gets a little more time (a
  quarter of the budget, at most 5 s).
- **Isolated and free.** The tables live in a private in-memory DuckDB with file and network access disabled and
  the configuration locked. Your data is never read and no query runs on your connection. No budget is spent.
- **Multiset comparison.** Row order does not count, duplicates do. Values are compared as text, so a NULL equals
  a NULL and NaN equals NaN. When only one of the original and the rewrite fails, that is a difference. When both
  fail, the case agrees but tested nothing, so it is not counted in `cases` and the result says how many there
  were (`both_failed`, and in `meaning`). Each query runs on fresh tables after one fails, so a failing query
  never makes the next one fail.

## Limits

- **DuckDB semantics.** BigQuery, Snowflake and Postgres SQL is translated to DuckDB first. If the translator
  warns, or the query uses a function it does not know, verification is skipped rather than testing a different
  query. BigQuery is tested in UTC. Engine details (collations, NULL ordering, float formatting) can still
  differ.
- **Skipped on purpose:** random functions and TABLESAMPLE; `ROW_NUMBER`/`LAG`/`FIRST_VALUE` ... windows (ties are
  arbitrary); `STRING_AGG`/`ARRAY_AGG`/`ANY_VALUE` without ORDER BY; `DISTINCT ON`; a LIMIT inside a subquery;
  table functions and files (`read_parquet(...)`). An outer LIMIT/OFFSET is removed from both queries before the
  comparison (it only cuts the same rows), and the result says so.
- **Not tested:** `GV002` (adding a LIMIT changes the result by design) and `GV007` (an approximate count is
  exact on tiny tables, so a pass would mean nothing).
- **Inferred schemas are a guess.** Without metadata, a column that is only selected gets INTEGER, and only
  date-like columns are tried in several types. Pass a connection (`--dsn` / `gulliver.toml`) for real types.
- **Small data.** Up to 50 rows per table finds NULL, duplicate, boundary and time-zone bugs. It does not find
  bugs that need large data or rare values outside the pools.
