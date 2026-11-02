# Changelog

## Unreleased

- `gulliver fix -o/--output PATH`: write rewritten SQL to a file when there are changes ([#6](https://github.com/laaielichter/Gulliver/issues/6)).
- `gulliver receipt -o PATH` and `gulliver translate -o PATH` write the receipt / translated SQL to a file
  ([#12](https://github.com/laaielichter/Gulliver/issues/12), [#11](https://github.com/laaielichter/Gulliver/issues/11)).
- New lint rule GV013 `leading-wildcard-like`: `LIKE '%x'` cannot use an index or min/max statistics
  ([#5](https://github.com/laaielichter/Gulliver/issues/5)).
- `gulliver_fix` rewrites `DATE_TRUNC('quarter', ts) = '2024-04-01'` to a range on `ts` (same rows on DATE and
  TIMESTAMP columns; a literal that does not start a quarter is left alone) ([#4](https://github.com/laaielichter/Gulliver/issues/4)).
- Docs: a read-only Postgres role for Gulliver, with `statement_timeout` ([#7](https://github.com/laaielichter/Gulliver/issues/7)).

## 0.2.0 - 2026-11-02

- New `gulliver_fix` (MCP) / `gulliver fix` (CLI): rewrites SQL to be cheaper and labels every change with a
  guarantee (`same`, `assumes`, `nulls`, `changes`). By default only `same`: date filters such as
  `DATE(ts) = '...'`, `ts::date`, `DATE_TRUNC`, `YEAR`/`EXTRACT(YEAR ...)` become ranges on the raw column when the
  connection shows a DATE or TIMESTAMP-without-zone column. `mode=all` adds the same rewrite for unknown types
  (`assumes`), NOT IN → NOT EXISTS, approximate distinct counts, UNION ALL and a LIMIT. With a connection it
  estimates before and after.
- `gulliver_preflight` includes `autofix.fixed_sql` when a same-rows rewrite exists, so the agent gets the fix
  without an extra call.
- The MCP server reports its version to clients (mcp 2.x).
- npm launcher `gulliver-mcp` (`npx -y gulliver-mcp`), which runs the matching PyPI version through uv.
- `server.json` for the official MCP Registry; CONTRIBUTING.md and CODE_OF_CONDUCT.md.
- Claude Code plugin installs the MCP server from PyPI (`uvx gulliver-mcp`).

## 0.1.0 - 2026-11-01

First release.

Query rehearsal:
- Workflow: `status`, `inspect`, `lint`, `preflight`, `rehearse`, `run`, `receipt`, `approve`.
- Rehearsal ladder (1% → 3% → 10%), power-law extrapolation with confidence ranges, detection of super-linear growth,
  saturation cap for grouped results.
- 12 scale lint rules (GV001-GV012).
- Budget gate: per query, per day, bytes, runtime, rehearsal requirement and human approval.

Pipeline tools:
- `impact` / `lineage`: column-level lineage over SQL model folders (dbt Jinja supported) or a dbt manifest.
- `snapshot` / `drift`: schema drift per connection, mapped to impacted models.
- `diff`: key-based data diff inside the engines with MD5 buckets and a zoom-in pass; works across engines.
- `translate`: SQL between DuckDB, Postgres, BigQuery and Snowflake, with warnings and an optional target preflight.
- `incremental`: append / partition_overwrite / merge_recompute / not_safe advice, generated SQL and dbt config,
  measured savings.
- `check`: unique, not null, accepted values, range, freshness and row-count change in one scan (exit code 4 on fail).

Engines and integration:
- DuckDB and PostgreSQL tested (Postgres live in CI), BigQuery and Snowflake beta (tested with fakes).
- MCP server (stdio and Streamable HTTP), CLI with `--json`, OpenAI and Gemini tool export.
- Claude Code plugin and agent skill.
