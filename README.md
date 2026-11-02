<!-- mcp-name: io.github.laaielichter/gulliver -->

<p align="center">
  <a href="https://laaielichter.github.io/Gulliver/"><img src="https://raw.githubusercontent.com/laaielichter/Gulliver/main/docs/og.png" alt="Gulliver: test on tiny data before you run on giant data. A rehearsal on 1%, 3% and 10% predicts about 120 million rows at 100%." width="760"></a>
</p>

<h1 align="center">Gulliver</h1>

<p align="center"><b>Test on tiny data before you run on giant data.</b><br>
An open-source MCP server and CLI that gives AI agents a dress rehearsal for SQL: estimate the cost, fix the slow parts, rehearse on 1% → 3% → 10% of the data, predict what 100% will do, and only run the full query inside a budget.</p>

<p align="center">
  <a href="https://github.com/laaielichter/Gulliver/actions/workflows/ci.yml"><img src="https://github.com/laaielichter/Gulliver/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/gulliver-mcp/"><img src="https://img.shields.io/pypi/v/gulliver-mcp?label=pypi" alt="PyPI version"></a>
  <a href="https://www.npmjs.com/package/gulliver-mcp"><img src="https://img.shields.io/npm/v/gulliver-mcp?label=npm" alt="npm version"></a>
  <img src="https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-blue" alt="Python 3.10 to 3.13">
  <a href="https://github.com/laaielichter/Gulliver/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT license"></a>
  <a href="https://github.com/laaielichter/Gulliver/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22"><img src="https://img.shields.io/github/issues/laaielichter/Gulliver/good%20first%20issue?label=good%20first%20issues&color=7057ff" alt="Good first issues"></a>
</p>

<p align="center">
  <a href="https://laaielichter.github.io/Gulliver/">Website</a> ·
  <a href="#quick-start-2-minutes-no-warehouse-needed">Quick start</a> ·
  <a href="#install-the-mcp-server">Install</a> ·
  <a href="#auto-fix">Auto-fix</a> ·
  <a href="#how-it-compares">Compare</a> ·
  <a href="#faq">FAQ</a> ·
  <a href="#roadmap">Roadmap</a>
</p>

<p align="center">Works with <b>Claude Code</b>, <b>Codex</b>, <b>Cursor</b>, <b>Gemini CLI</b>, Claude Desktop, VS Code, Windsurf and any MCP client, on <b>DuckDB</b>, <b>Postgres</b>, <b>BigQuery</b> and <b>Snowflake</b>.</p>

> **New in 0.2.0:** `gulliver_fix` rewrites slow SQL for the agent and labels every change. "Same rows" is only
> promised when Gulliver has checked the column types. Also new: run it with `npx -y gulliver-mcp`, and the Claude Code
> plugin installs straight from PyPI. See the [changelog](https://github.com/laaielichter/Gulliver/blob/main/CHANGELOG.md).

![Gulliver rehearses a self-join on 1%, 3% and 10% of a 5-million-row table, predicts about 120 million output rows, then rewrites a date filter with the same rows](https://raw.githubusercontent.com/laaielichter/Gulliver/main/docs/demo.svg)

<sub>A real recording of the CLI on the demo database (script: [docs/demo.sh](https://github.com/laaielichter/Gulliver/blob/main/docs/demo.sh)).</sub>

> In *Gulliver's Travels*, Gulliver visits Lilliput, a land of tiny people, and Brobdingnag, a land of giants.
> Gulliver does the same with your data: it tries your query in Lilliput first, and only walks into Brobdingnag
> when it knows what will happen there.

## Why

Agents write SQL that works on a small table and breaks on a big one. They do not know that `events` has
5 billion rows, that a join multiplies rows, or that a forgotten partition filter scans every day since 2019.
Benchmarks score agents on *is the answer right*, never on *what did it cost* or *does it still work at 100x*.
One unlucky query can burn a monthly budget.

Gulliver sits between the agent and the warehouse:

| Step | Tool | What happens |
|---|---|---|
| 1 | `gulliver_inspect` | Which tables are big? Rows, bytes, partitions, columns. Metadata only, free. |
| 2 | `gulliver_preflight` | Bytes, rows and money **before** running, plus **scale lint** (13 rules) and the budget gate. When a safe rewrite exists, the answer already contains the fixed SQL (`autofix`). |
| 3 | `gulliver_rehearse` | Runs the query on 1%, 3% and 10% samples, fits a growth curve and **predicts the full run** with a confidence range. Catches joins that multiply rows. |
| 4 | `gulliver_run` | Runs the full query only inside the budget. Otherwise answers `needs_rehearsal` or `needs_approval`. |
| 5 | `gulliver_receipt` | Markdown proof for the human: what was estimated, rehearsed, approved and run, and predicted vs actual. |

Every answer has a `status` and a `next_step`, so even small models know what to do next.

Gulliver does not only say no. `gulliver_fix` **rewrites the SQL for the agent** and labels every change.
`same` (exactly the same rows) is only claimed when Gulliver has checked the column types on the connection;
everything else (`assumes`, `nulls`, `changes`) needs `mode=all` and says what may differ. With a connection it
estimates the old and the new query side by side, so nobody has to trust the saving blindly.

And for the pipeline around the query (details in [Pipeline tools](#pipeline-tools)):

| Tool | Problem it solves |
|---|---|
| `gulliver_impact` | "If I change or drop this column, what breaks?" Column-level lineage over SQL models or a dbt manifest. |
| `gulliver_drift` | Schema changed under you: columns removed, retyped or added since the last snapshot, plus the models they break. |
| `gulliver_diff` | "Does my refactored / migrated / incremental model return the same rows?" Key-based data diff inside the engine, also across engines. |
| `gulliver_translate` | SQL between DuckDB, Postgres, BigQuery and Snowflake, with warnings and an optional preflight on the target. |
| `gulliver_incremental` | A model rebuilds everything every run. Is incremental safe, what is the SQL / dbt config, and how many rows does it skip? |
| `gulliver_check` | Data quality in one cheap scan: unique keys, not null, accepted values, ranges, freshness, row-count jumps. |

## Quick start (2 minutes, no warehouse needed)

```bash
pip install gulliver-mcp
gulliver demo                       # creates gulliver_demo.duckdb with 5M events
export GULLIVER_DSN=gulliver_demo.duckdb

gulliver inspect
gulliver preflight "select country, count(*) from events e join users u on e.user_id = u.user_id group by 1"
gulliver lint "select * from events e, users u"           # GV003: join without a condition -> blocked
gulliver rehearse "select a.event_id, b.event_id from events a join events b on a.user_id = b.user_id"
```

The last one is the classic agent mistake: a self-join on a non-unique key. The rehearsal sees the output grow
with exponent ~1.8 instead of ~1 and warns that the full result has more than 100 million rows, before you run it.

```
| Sample | Runtime  | Output rows |
|--------|----------|-------------|
| 1%     | 0.0202s  | 41,706      |
| 3%     | 0.0484s  | 237,422     |
| 10%    | 0.3909s  | 2,438,395   |

Prediction for 100%: output rows 134,525,654 (range 67,262,827 - 269,051,308)
Growth exponent output_rows: 1.77 -> grows faster than the data
```

Add `--json` to any command for machine-readable output. Exit codes: `0` ok, `1` error, `2` blocked,
`3` needs rehearsal or approval, `4` a data check failed, so it also works as a CI check.

## Install the MCP server

Gulliver is an MCP server (stdio or Streamable HTTP), published on [PyPI](https://pypi.org/project/gulliver-mcp/)
and [npm](https://www.npmjs.com/package/gulliver-mcp). Run it with [uv](https://docs.astral.sh/uv/), or through npx
(which uses uv under the hood):

```bash
uvx gulliver-mcp --dsn path/to/warehouse.duckdb
npx -y gulliver-mcp --dsn path/to/warehouse.duckdb
```

Point it at a database with `--dsn`, or at a [`gulliver.toml`](#configuration) with connections and budgets
(`--config`). No warehouse yet? Run `gulliver demo` and use `gulliver_demo.duckdb`.

### Claude Code

```bash
claude mcp add gulliver -- uvx gulliver-mcp --dsn /abs/path/data.duckdb
```

Or install it as a plugin, which adds the MCP server and a skill with the rehearsal workflow:

```
/plugin marketplace add laaielichter/gulliver
/plugin install gulliver@gulliver
```

### Claude Desktop, Cursor and Windsurf

Add this to the client's MCP config (`claude_desktop_config.json`, `~/.cursor/mcp.json` or
`~/.codeium/windsurf/mcp_config.json`):

```json
{
  "mcpServers": {
    "gulliver": {
      "command": "uvx",
      "args": ["gulliver-mcp", "--config", "/abs/path/gulliver.toml"]
    }
  }
}
```

### VS Code

In `.vscode/mcp.json`:

```json
{
  "servers": {
    "gulliver": {
      "type": "stdio",
      "command": "uvx",
      "args": ["gulliver-mcp", "--config", "/abs/path/gulliver.toml"]
    }
  }
}
```

### Codex CLI

In `~/.codex/config.toml`:

```toml
[mcp_servers.gulliver]
command = "uvx"
args = ["gulliver-mcp", "--config", "/abs/path/gulliver.toml"]
```

### Gemini CLI

In `~/.gemini/settings.json`, the same `mcpServers` block as for Claude Desktop above.

### Any HTTP client or remote agent

`gulliver-mcp --http --port 8765` serves `http://127.0.0.1:8765/mcp` (Streamable HTTP).

### Without MCP: OpenAI and Gemini function calling

Every tool is also a CLI command with `--json`, and `gulliver tools --format openai` or `--format gemini` prints
ready-made function-calling definitions for the OpenAI or Gemini APIs.

## How it compares

Based on each project's public docs in October 2026; open an issue if something here is wrong or out of date.

| | Gulliver | cost-guard-mcp | Database MCP servers (MCP Toolbox, vendor MCPs) | dbt-mcp / dbt `--sample` | Altimate Code |
|---|---|---|---|---|---|
| Cost estimate before running | ✓ (exact on BigQuery, upper bound on Snowflake, heuristic on DuckDB/Postgres) | ✓ BigQuery, Snowflake, Databricks | limits and timeouts, no estimate | – | FinOps tools (Snowflake-focused) |
| Rehearse on 1%/3%/10% and predict the full run | ✓ | – | – | dbt only, time windows only | – |
| Scale lint with an auto-fix that proves "same rows" | ✓ | – | – | – | SQL review |
| Budget gate + human approval + receipt | ✓ | – | read-only modes | – | – |
| Impact, drift, data-diff, translate, incremental, checks | ✓ | – | – | lineage | many tools |
| Engines | DuckDB, Postgres, BigQuery (beta), Snowflake (beta) | BigQuery, Snowflake, Databricks | many | dbt projects | Snowflake, dbt and more |
| Runs locally, open source | ✓ MIT | ✓ | ✓ | ✓ | ✓ MIT |

What Gulliver does **not** do: run your pipelines, replace dbt, or give you a hosted service. There is no
Databricks or ClickHouse engine yet, and BigQuery and Snowflake have only been tested against fakes so far.

## Engines

| Engine | Estimate before running | Sampling | Money model | Status |
|---|---|---|---|---|
| DuckDB (files, Parquet, CSV, in-memory) | EXPLAIN + metadata (heuristic) | `TABLESAMPLE` system/bernoulli, seeded | free (time is the budget) | tested in CI |
| BigQuery | dry run: **exact** bytes | `TABLESAMPLE SYSTEM` | on-demand $/TiB, plus a hard `maximum_bytes_billed` cap | beta, tested with fakes |
| Snowflake | `EXPLAIN USING JSON`: bytes and partitions (upper bound) | `SAMPLE SYSTEM ... SEED` | warehouse credits x runtime | beta, tested with fakes |
| PostgreSQL | `EXPLAIN (FORMAT JSON)` (heuristic), partition-aware sizes | `TABLESAMPLE ... REPEATABLE` | self-hosted (time and bytes are the budget) | tested in CI against a live Postgres 16 |

Install the drivers you need: `pip install "gulliver-mcp[bigquery]"`, `[snowflake]`, `[postgres]` or `[all]`.
Every estimate says how much to trust it: `exact`, `upper_bound` or `heuristic`. A heuristic is never called exact.

## Configuration

Copy [`gulliver.example.toml`](https://github.com/laaielichter/Gulliver/blob/main/gulliver.example.toml) to `gulliver.toml`:

```toml
default_connection = "local"

[connections.local]
engine = "duckdb"
path = "warehouse.duckdb"

[connections.bq]
dsn = "bigquery://my-project?location=EU"
dataset = "analytics"
price_per_tib_usd = 6.25

[budget]
max_query_usd = 1.00        # hard stop for one query
approval_over_usd = 0.25    # above this a human runs `gulliver approve <receipt_id>`
rehearse_over_usd = 0.10    # above this a rehearsal is required first
day_usd = 10.00             # total per day (local ledger)
max_runtime_s = 900
max_output_rows = 200       # rows handed back to the agent
```

### A read-only Postgres role

Gulliver refuses writes itself, but a role that can only read is a good second layer. Run this once as an admin
and put `gulliver_ro` in the DSN:

```sql
CREATE ROLE gulliver_ro LOGIN PASSWORD 'change-me';
GRANT CONNECT ON DATABASE app TO gulliver_ro;
GRANT USAGE ON SCHEMA public TO gulliver_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO gulliver_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO gulliver_ro;  -- tables created later
ALTER ROLE gulliver_ro SET statement_timeout = '15min';           -- the server stops runaway queries too
ALTER ROLE gulliver_ro SET default_transaction_read_only = on;
```

Or skip the file: `GULLIVER_DSN`, `GULLIVER_MAX_QUERY_USD`, `GULLIVER_DAY_USD` and friends.
Credentials never go in the config: Gulliver uses each engine's own environment variables and credential chains
(Google ADC, `SNOWFLAKE_PASSWORD`, `PGPASSWORD`, ...). The agent only ever sees connection *names*.

## Safety model

- **Read-first.** Only `SELECT` (and `WITH ... SELECT`) runs by default. `DELETE`, `UPDATE`, `DROP`, `TRUNCATE`,
  `ALTER`, `MERGE`, several statements at once and unparsable SQL are refused by a parser, not a regex.
- **Writes need two keys.** `CREATE TABLE ... AS SELECT` and `INSERT ... SELECT` run only when the call says
  `allow_write` **and** a human set `allow_write = true` on that connection. Rehearsals never write.
- **Budgets fail closed.** Over the per-query, per-day, bytes or runtime limit means `block`. Money above the
  approval limit needs `gulliver approve <receipt_id>`, a CLI command for humans that is not exposed as an MCP tool.
- **Engine-side caps** where they exist: BigQuery `maximum_bytes_billed`, Snowflake and Postgres statement timeouts,
  a DuckDB interrupt timer.
- **Local state only.** Receipts, the spend ledger and approvals live in `./.gulliver/` as JSON. No telemetry.

## How the prediction works

Each rung measures runtime, output rows and (where the engine reports it) bytes. Gulliver fits
`metric = a * fraction^k` in log-log space and reads off the value at 100%.

- `k ≈ 1`: grows with the data (scans, filters, key joins against a full table).
- `k ≈ 0`: flat (aggregations). Group counts saturate, so grouped results are capped by what was observed and the planner estimate.
- `k > 1.3`: grows faster than the data: a join that multiplies rows, a big sort, or both join sides sampled. Flagged.

Runtime first subtracts the fixed per-query overhead (measured with `SELECT 1`). Confidence is `high`, `medium`
or `low`, with a range of x0.7-1.5, x0.5-2 or x0.25-4. After a real run, the receipt shows predicted vs actual.

By default only the **largest** table is sampled and the others stay complete, which keeps key joins honest.
`--sample all` samples every table. For BigQuery the money prediction uses the exact dry-run bytes of the full query.

**Limits, honestly:** block (system) sampling is cheap but can be skewed on clustered data; use `--method bernoulli`
when that matters. Extrapolating 10x always has uncertainty; Gulliver shows it instead of hiding it. Databricks,
ClickHouse and Spark are not supported yet.

## Pipeline tools

These reuse the same connections, budget gate and receipts. Every query they run is generated, parsed,
estimated and gated like your own queries.

### Impact: what breaks if a column changes

```bash
gulliver impact raw.events.amount --models models/        # or --manifest target/manifest.json
```
```
Changing raw.events.amount affects 4 model(s):
  stg_events: amount  <- raw.events.amount
    fct_revenue: revenue  <- stg_events.amount
    user_ltv: ltv  <- stg_events.amount
      country_report: revenue  <- fct_revenue.revenue
```

Plain `*.sql` files and dbt models (`{{ ref() }}`, `{{ source() }}`, `{{ config() }}`, `{% if %}`) both work.
Columns used only in filters or joins are reported too, and `SELECT *` models are flagged because their shape
follows the source. `gulliver lineage --models models/` prints the whole graph.

### Drift: schema changes since last time

```bash
gulliver snapshot                          # remember tables, columns and row counts
gulliver drift --models models/            # later: what changed, and which models it breaks
gulliver drift --save                      # accept the new schema
```

Removed and retyped columns are `warn` and are mapped to impacted models; row counts that move more than 20%
are listed too.

### Diff: prove two queries return the same rows

```bash
gulliver diff --left "select * from fct_orders" --right "select * from fct_orders_v2" --key order_id
gulliver diff --left orders --right orders --key id -c postgres_prod --right-connection snowflake_new
```

Each side is reduced inside its engine to 256 buckets of (row count, sum of MD5 row hashes) keyed by the key's
hash, so it scales to billions of rows and costs about one scan per side. Only buckets that differ are opened
(with a finer second pass when they are big) to list keys that are missing, extra or changed. MD5 is the same on
every engine, so you can diff across engines; values are compared as text, so cast or round in your queries where
engines format numbers or timestamps differently. Use it after a refactor, a migration or an incremental switch.

### Translate: SQL between dialects

```bash
gulliver translate --from snowflake --to bigquery -f model.sql
gulliver translate --from duckdb --to postgres --check -c pg "select ..."   # also preflight on the target
```

Functions the translator does not know are copied as-is and listed as warnings, so nothing is silently wrong.

### Incremental: stop rebuilding everything

```bash
gulliver incremental -f models/user_ltv.sql --time-column ts --unique-key user_id --period 1d
```

| Strategy | When | What it generates |
|---|---|---|
| `append` | no aggregation | read only rows newer than the target's max (merge on `unique_key` for late updates) |
| `partition_overwrite` | grouped by a time grain | dbt `microbatch` config: recompute the recent periods (+ lookback) |
| `merge_recompute` | grouped (or windowed) by an entity key | recompute only the keys that got new rows, merge on the key |
| `not_safe` | LIMIT, totals over everything, windows without PARTITION BY | keeps the full rebuild and says why |

It also measures the saving on the real source ("each run would read about 172,800 of 5,000,000 rows, 96.5% fewer")
and, on billed engines, compares dry-run bytes, telling you when you need to partition or cluster the source to
turn fewer rows into less money. Prove the result with `gulliver diff`.

### Check: data quality in one scan

```bash
gulliver check events --unique event_id --not-null user_id,amount \
  --accepted "event_type=view|click|cart|buy" --range "amount=0:10000" --fresh ts=24h --max-change 30
```

All checks become one aggregate query (one scan), budget-gated like any other. Status `fail` (exit code 4)
when a check fails, so it drops into CI or an orchestrator. Row-count history is kept per target, so
`--max-change` catches a load that suddenly lost half its rows.

## Auto-fix

```bash
gulliver fix "select event_type, count(*) from events where date(ts) = '2025-01-02' group by 1"
```

```
GV005 [returns exactly the same rows]
  before: CAST(ts AS DATE) = '2025-01-02'
  after:  ts >= '2025-01-02' AND ts < '2025-01-03'
  why:    Filters the raw column ts by range, so partitions, indexes and min/max statistics can skip data. ts is TIMESTAMP, so the rows are the same.
```

| Rewrite | Rule | Guarantee | Default |
|---|---|---|---|
| `DATE(ts) = d`, `ts::date`, `TO_DATE(ts)`, `DATE_TRUNC(day/month/quarter/year, ts)`, `YEAR(ts)`, `EXTRACT(YEAR ...)` with `=`, `<`, `<=`, `>`, `>=`, `BETWEEN` → a range on the raw column | GV005 | `same` when the connection shows the column is a DATE or a TIMESTAMP without time zone | yes |
| the same, when the column type is unknown | GV005 | `assumes` (same rows only if it is such a column) | `mode=all` |
| `a.x NOT IN (SELECT y ...)` → `NOT EXISTS` | GV012 | `nulls`: same rows unless the subquery returns NULL | `mode=all` |
| `COUNT(DISTINCT x)` → approximate distinct count | GV007 | `changes` (approximate) | `mode=all` |
| `UNION` → `UNION ALL` | GV009 | `changes` (keeps duplicates) | `mode=all` |
| unbounded `SELECT` → `LIMIT n` | GV002 | `changes` (fewer rows) | `mode=all` |

`same` is a promise, so Gulliver keeps it narrow. It never rewrites a date filter on text columns,
`TIMESTAMP WITH TIME ZONE` (a day can start twice when the clocks change at midnight), intervals, `TRY_` and
`BETWEEN SYMMETRIC` variants, filters with a time-zone or format argument, or periods that run past year 9999.
A column only counts as typed when it clearly comes from one table with metadata, not from a CTE or subquery
that could redefine it. Every `same` rewrite is tested against real data: DATE and TIMESTAMP columns, NULLs,
`infinity`, the last microsecond of a day and leap days.

What it will not do: guess a date range for a missing partition filter (GV004) or a join key for a
cartesian join (GV003). Those need a decision, so they stay in `remaining`.

Where it pays off: Postgres cannot use a plain index or range partitions on `ts` for `ts::date = ...`, and
BigQuery, Snowflake and Parquet readers prune best on the raw column. DuckDB already optimizes this case on its
own tables, so there the estimate stays the same.

## Scale lint rules

| Rule | Name | Severity |
|---|---|---|
| GV001 | select-star | warn |
| GV002 | unbounded-output | warn |
| GV003 | cartesian-join | error (no condition) / warn (CROSS JOIN) |
| GV004 | missing-partition-filter | error |
| GV005 | function-on-filter-column | warn |
| GV006 | order-without-limit | warn |
| GV007 | exact-count-distinct | info |
| GV008 | non-equi-join | warn |
| GV009 | union-distinct | info |
| GV010 | scalar-subquery-in-select | info |
| GV011 | window-without-partition | warn |
| GV012 | not-in-subquery | info |
| GV013 | leading-wildcard-like | info |

`gulliver lint` works without any database.

## CLI reference

```
gulliver status                       connections, budget, spend today
gulliver inspect [TABLE]              biggest tables, or one table's columns
gulliver lint SQL [--dialect D]       scale lint only
gulliver fix SQL [--dialect D] [--mode safe|all] [--limit N] [-o PATH]   rewrite SQL, say what changes
gulliver preflight SQL                estimate + lint + gate, no execution
gulliver rehearse SQL [--fractions 0.01,0.03,0.1] [--sample largest|all] [--method auto|system|bernoulli]
gulliver run SQL [--max-rows N] [--allow-write]
gulliver receipt [ID|latest] [--format markdown|json] [-o PATH]
gulliver approve ID [--note TEXT]     (human only)
gulliver demo [PATH]                  create a demo DuckDB database
gulliver lineage --models DIR | --manifest FILE
gulliver impact TABLE[.COLUMN] --models DIR | --manifest FILE
gulliver snapshot | drift [--models DIR] [--save]
gulliver diff --left SQL --right SQL --key k1,k2 [--right-connection NAME] [--columns a,b]
gulliver translate SQL --to D [--from D] [--check] [-o PATH]
gulliver incremental SQL [--time-column C] [--unique-key K] [--period 1d] [--lookback 1]
gulliver check TARGET [--where W] [--unique K] [--not-null C] [--accepted C=a|b] [--range C=min:max] [--fresh C=24h] [--min-rows N] [--max-change PCT]
gulliver tools --format mcp|openai|gemini
gulliver mcp [--http]                 start the MCP server
```

Exit codes: `0` ok, `1` error, `2` blocked, `3` needs rehearsal or approval, `4` a data check failed.

SQL can be an argument, `-f file.sql`, or `-` for stdin. Global flags: `--dsn`, `--config`, `-c NAME`, `--json`.

## FAQ

**Does Gulliver send my data or my SQL anywhere?**
No. It runs on your machine and only talks to the databases you configure. Receipts, the spend ledger and
approvals are JSON files in `./.gulliver/`. There is no telemetry.

**Do I need a warehouse to try it?**
No. `gulliver demo` builds a DuckDB database with 5 million events in seconds, and `gulliver lint` works without
any database at all.

**Which agents and models does it work with?**
Any MCP client: Claude Code, Codex, Cursor, Gemini CLI, Claude Desktop, VS Code, Windsurf. Without MCP, use the CLI
with `--json`, or `gulliver tools --format openai|gemini` for function calling. Every answer is short JSON with a
`status` and a `next_step`, so smaller models can follow the loop too.

**How accurate is the prediction?**
It is a growth curve fitted on three samples, so it comes with a confidence level and a range (x0.7-1.5 when
confidence is high, up to x0.25-4 when it is low). On BigQuery the money estimate uses the exact dry-run bytes of the
full query. After a real run, the receipt shows predicted versus actual. Joins and skewed data can grow faster than
the data; Gulliver flags that (growth exponent above 1.3) instead of hiding it.

**Can the agent change or delete data?**
Not by default. Only `SELECT` runs. Writes (`CREATE TABLE ... AS SELECT`, `INSERT ... SELECT`) need both the call
and a human-set `allow_write = true` on the connection, and the agent cannot approve its own runs.

**How is this different from `EXPLAIN` or a BigQuery dry run?**
Gulliver uses those for the preflight. On top of that it rehearses on samples to show how runtime and output rows
grow, rewrites slow SQL with a stated guarantee, enforces a budget, and writes a receipt, in one JSON format across
four engines.

**Is it ready for production on BigQuery and Snowflake?**
Not yet. Both are beta and have been tested against fakes, not live accounts. DuckDB and Postgres are tested in CI,
Postgres against a live server. Reports from real warehouses are very welcome.

## Roadmap

Ideas for what comes next, not promises. Each one is open for help:

- Live tests for BigQuery and Snowflake, so they can leave beta.
- More same-rows rewrites in `gulliver_fix`.
- Databricks and ClickHouse engines.

Have a case where an agent's SQL went wrong at scale? [Open an issue](https://github.com/laaielichter/Gulliver/issues/new/choose).
Real examples shape the next lint rules.

## Contributing

```bash
git clone https://github.com/laaielichter/Gulliver && cd Gulliver
pip install -e ".[dev]"
pytest
```

Contributions are welcome. Start with a [good first issue](https://github.com/laaielichter/Gulliver/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22),
and read [CONTRIBUTING.md](https://github.com/laaielichter/Gulliver/blob/main/CONTRIBUTING.md) and the
[Code of Conduct](https://github.com/laaielichter/Gulliver/blob/main/CODE_OF_CONDUCT.md).
See [AGENTS.md](https://github.com/laaielichter/Gulliver/blob/main/AGENTS.md) for the agent-facing guide and
[SECURITY.md](https://github.com/laaielichter/Gulliver/blob/main/SECURITY.md) to report a vulnerability.

Thanks to everyone who helps:

<a href="https://github.com/laaielichter/Gulliver/graphs/contributors"><img src="https://contrib.rocks/image?repo=laaielichter/Gulliver" alt="Contributors to Gulliver"></a>

## License

MIT, see [LICENSE](https://github.com/laaielichter/Gulliver/blob/main/LICENSE).
