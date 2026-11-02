# `gulliver review`: a PR gate for SQL models

**The problem.** A pull request that drops a column, adds a join that multiplies rows or doubles a scan looks
harmless in the diff. Reviewers work out by hand what changed and what breaks, and the cost shows up on next
month's bill. Existing tools cover one piece each (BigQuery dry-run bytes, dbt-only lineage diffs, paid PR
comments). `gulliver review` puts lint, change type, downstream impact, cost and row growth for plain SQL and dbt
models into one report and one exit code.

For every `.sql` model that changed between a base ref and head, it reports:

- **New scale lint**: findings in the new version that the old one did not have (GV002 "no LIMIT" is skipped:
  a model is meant to return all its rows).
- **Change type**: `cosmetic` (same SQL after normalizing formatting, comments, identifier case and table
  aliases; aliases are kept when two tables share a name, such as `prod.orders` and `archive.orders`),
  `non_breaking` (only output columns added), or `breaking` (a column removed, retyped or computed
  differently, an unaliased expression compared by position as `_col_<n>`, or WHERE / JOIN / GROUP BY / other row clauses changed, or the model deleted or renamed).
- **Downstream impact**: which models use the removed or changed columns (or the whole model, when its rows can
  change), from column-level lineage of the head version.
- **Cost, with a connection**: both versions are preflighted (estimate only, nothing runs). It shows the scan and
  money per run before and after, and the change per month (`runs_per_day` x 30).
- **Row growth, with `--rehearse`**: both versions run on 1% and 3% samples. If output rows now grow faster than
  the data (exponent above 1.3, and it was not before), it raises a `superlinear` flag.

## Commands

```bash
# git: compare the branch with main (only committed changes)
gulliver review --models models --base origin/main

# with cost estimates on a warehouse (refs resolve to production tables, like dbt --defer)
gulliver --dsn bigquery://my-project review --models models --base origin/main

# also catch new row explosions on 1% and 3% samples (budget-checked; costs money on billed engines)
gulliver --dsn warehouse.duckdb review --models models --base origin/main --rehearse

# dbt: compare two compiled manifests instead of git
gulliver review --manifest-base prod/manifest.json --manifest-head target/manifest.json

# write the PR comment to a file, print JSON for scripts
gulliver review --models models --base origin/main -o review.md --json > review.json
```

Exit codes: `0` ok or warn, `1` error, `2` block (do not merge). `--format json -o file` writes the JSON report
instead of markdown. MCP: `gulliver_review` (same arguments; `markdown=true` also returns the comment text), for
agents to call before they open a PR.

## Policy: `[review]` in gulliver.toml

```toml
[review]
max_increase_pct = 100      # scan per run, per model; only for scans of at least ignore_below_bytes
ignore_below_bytes = 1073741824
max_increase_usd = 50       # dollars PER MONTH for the whole change (per run x runs_per_day x 30)
runs_per_day = 1
fail_on = ["cost", "breaking_with_downstream", "new_lint_error", "superlinear"]
# ref_schema = "analytics"  # {{ ref('x') }} becomes analytics.x when estimating
```

Every finding is a flag of one kind: `cost`, `breaking`, `breaking_with_downstream`, `new_lint_error`,
`new_lint_warning`, `superlinear` or `not_compiled`. A flag whose kind is in `fail_on` blocks (`breaking` also
covers `breaking_with_downstream`; `new_lint_warning` also covers `new_lint_error`). Other flags give `warn`.
`--fail-on`, `--max-increase-pct`, `--max-increase-usd` and `--runs-per-day` override the file for one run;
`--fail-on none` reports without blocking.

## Example report

```markdown
<!-- gulliver-review -->
## Gulliver review: BLOCK

`main` (e20bf1cb) ... `feature` (c99080c4) - 3 model(s) changed - scan +2.3 MB per run - $0 per month at 1 run(s)/day (heuristic)

**Blocks the merge**

- `breaking_with_downstream` user_totals: changes FROM, GROUP BY, JOIN: rows can be added, dropped or duplicated; removes column(s) n, total; used by 1 downstream model(s): report
- `superlinear` user_totals: output rows now grow faster than the data, exponent 1.79 (was 0.35); often a join that multiplies rows

**Worth a look**

- `new_lint_warning` user_totals: new warn GV001 select-star: SELECT * reads every column. ...

| Model | Change | Downstream | Scan per run | $ per month | New lint | Growth |
|---|---|---|---|---|---|---|
| `country` | cosmetic (modified) | 0 | - | - | - | - |
| `extra` | non_breaking (modified) | 0 | 15.6 KB -> 15.6 KB (+0%) | $0 | - | skipped |
| `user_totals` | breaking (modified) | 1 | 1.4 MB -> 3.7 MB (+167%) | $0 | GV001 | **rows ^1.79 (was ^0.35)** |
```

Each changed model also gets a collapsed details block (reasons, downstream columns, lint fixes, cost per run).

## GitHub Action

`action.yml` at the repository root is a composite action. It installs Gulliver from the action's own source (or
`package:`), runs the review, writes the report to the job summary, posts or updates **one** PR comment (its own: written by
the token's user, `github-actions[bot]` for the default token, and starting with the `<!-- gulliver-review -->`
marker; a comment someone else writes with the marker is never touched), optionally uploads SARIF, and fails the job on `block`.

```yaml
on: pull_request
permissions:
  contents: read
  pull-requests: write      # for the comment
  # security-events: write  # only with sarif: true
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0     # review needs the base branch history
      - uses: laaielichter/Gulliver@main   # pin a release tag or commit
        with:
          models: models
          # dsn: bigquery://my-project     # cost estimates; credentials come from the job env
          # extras: bigquery
          # rehearse: "true"
          # sarif: "true"                  # needs `gulliver lint --format sarif`
```

Inputs reach the shell only through environment variables (no `${{ }}` inside scripts). On fork PRs the token is
read-only: the comment step warns and the gate still runs.

## Guarantees

- No base, no review: a missing base ref, no shared history (shallow clone) or a missing, empty or foreign base
  manifest gives `block`, never "review everything". A `--models` folder that is in neither the base, the head nor
  the checkout (a typo) gives `block` too, never "no SQL model changed".
- Without `--rehearse` nothing runs: cost comes from the engine's estimate (BigQuery dry run, EXPLAIN elsewhere)
  and goes through the same preflight as `gulliver preflight`. With `--rehearse`, sample runs go through the same
  budget checks and ledger as `gulliver rehearse`.
- Agents cannot change the policy through MCP; only a human edits `[review]`.

## Limits (honest)

- Only committed changes are reviewed; uncommitted `.sql` changes are named in the notes.
- dbt Jinja beyond `ref`, `source`, `config` and `this` is stripped, not compiled: those models are marked
  `compiled: approximate`. Use `--manifest-base/--manifest-head` with compiled manifests for exact SQL. A model
  that still does not parse is `unknown` with a `not_compiled` flag.
- Classification is static (sqlglot). It cannot prove that a new LEFT JOIN on a unique key keeps the rows, so it
  calls that breaking. Use `gulliver diff` to prove two versions return the same rows.
- Column types are only compared when both versions use `CAST`; other type changes show up as "computed
  differently".
- Cost accuracy follows the engine: BigQuery is exact, Snowflake is an upper bound on bytes (no money without a
  run), DuckDB and Postgres are heuristic. Each model and the total carry an `accuracy` label. Estimates use the
  production tables of today; a column added upstream in the same PR is not there yet, so that model shows "not
  estimated".
- Rehearsal uses two sample sizes, which gives a rough exponent, not a forecast. Self-joins where both sides are
  sampled can look superlinear even on a unique key; the flag says "often", not "always".
- Manifest mode cannot see renames (a renamed model is removed + added).
- The SARIF step depends on `gulliver lint --format sarif` for a folder; on versions without it the step warns
  and is skipped.
