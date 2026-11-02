# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub:
**Security tab → Report a vulnerability** on https://github.com/laaielichter/gulliver.
Do not open a public issue for a vulnerability.

You can expect a first answer within 7 days. Fixed issues are published as a GitHub security advisory.

## Supported versions

Only the latest release gets security fixes.

## What Gulliver is designed to prevent

Gulliver stands between an AI agent and a data warehouse. Its security model:

- Only read statements run by default. Destructive statements (DELETE, UPDATE, DROP, TRUNCATE, ALTER, MERGE)
  and multi-statement input are refused by an SQL parser.
- Writes (CREATE/INSERT ... SELECT) need both the call flag and `allow_write = true` on the connection.
- Budgets fail closed. Approvals are a human-only CLI command, not an MCP tool.
- Credentials are never stored by Gulliver and never shown to the agent (DSN passwords are redacted).
- Local state (`.gulliver/`) holds SQL and result previews; it ships with its own `.gitignore`.

Bypasses of any of these (for example SQL that writes data while classified as a read, or a way for the agent to
approve its own run) are in scope and very welcome reports.

## Threat model

**Who we defend against.** An AI agent that writes and sends SQL through Gulliver's tools, and anything that
steers that agent (a prompt injection hidden in data, a document or a web page). The agent can call every MCP
tool with any SQL text. It cannot edit `gulliver.toml`, set environment variables or run `gulliver approve`.

**Who we trust.** The human who installs Gulliver and writes `gulliver.toml`, the database credentials they
configure, and the database server itself.

**What the agent must not be able to do through Gulliver:**

| Risk | How Gulliver stops it | Where |
|---|---|---|
| Change or delete data | Only one SELECT (or CREATE/INSERT ... SELECT with two human switches) is accepted; a parser refuses everything else, several statements at once, DML hidden in a CTE, `SELECT INTO` | `sqlsafety.classify` |
| Stop or reconfigure the server (`pg_terminate_backend`, `pg_reload_conf`, `SYSTEM$CANCEL_QUERY`, ...) | GS001 server-control: refused by function name | `security.check` |
| Change session or database state (`set_config`, advisory locks, `nextval`, `pg_notify`, large objects, `FOR UPDATE`) | GS002 session-state | `security.check` |
| Run SQL the parser never sees (`dblink`, `query_to_xml`, `ts_stat`, DuckDB `query()`, BigQuery `EXTERNAL_QUERY`) | GS003 dynamic-sql | `security.check` |
| Read server files, environment variables, secrets or other databases (`pg_read_file`, `lo_import`, `read_text`, `getenv`, `duckdb_secrets`, `postgres_scan`, `http_get`) | GS004 file-access: refused by name | `security.check` |
| Read local files or URLs outside the folders the human allowed (DuckDB `read_csv('/etc/passwd')`, `FROM 's3://...'`, `../`, symlinks, `~`, hidden files such as `.env` or `.gulliver/`) | GS004 file-access: path allowlist in the parser, **and** DuckDB's own sandbox (`allowed_directories`, `enable_external_access = false`, no extension auto-load, `lock_configuration`) | `security.check_path`, `security.sandbox_duckdb` |
| Spend too much | Budgets and approvals (see above) | `core._gate` |
| Approve its own run | Approval is a CLI command, not an MCP tool | `core.approve` |

Names are matched after lower-casing, removing the schema (`pg_catalog.`), removing quotes and decoding
`U&"..."` escapes. Comments, dollar quoting, CTEs, subqueries, `LATERAL`, `ROWS FROM`, UNION branches and the
SELECT part of a write are all checked. The checks apply to every SQL text that reaches an engine through
Gulliver: preflight, rehearse, run, diff, check, incremental savings and translate `--check`.

**The bypass test set.** `src/gulliver/security_corpus.toml` lists more than 130 cases: known bypasses that must
be refused (with their code) and ordinary SQL that must stay allowed. CI runs every case
(`tests/test_security.py`), and anyone can run it, plus a live check of the DuckDB sandbox, with:

```bash
gulliver selftest security          # exit 0 when every case and sandbox check passes, 4 when one fails
```

**What Gulliver does not defend against (use the database for these):**

- **Functions and views defined in the database.** A view or a user function may call `pg_read_file`, `dblink`
  or anything else; Gulliver sees only the name you call. On Postgres use a role that cannot create or own
  functions, and check `SECURITY DEFINER` functions and untrusted languages (`plpython3u`, `plperlu`).
- **A denylist is never complete.** New engine versions and extensions add functions. The DuckDB sandbox does
  not depend on names, but for Postgres, BigQuery and Snowflake the parser is the only Gulliver-side check.
  **Give Gulliver a read-only database role** (Postgres `pg_read_all_data` or plain `SELECT` grants; a BigQuery
  service account with `roles/bigquery.dataViewer` + `jobUser`; a Snowflake role with only `SELECT`).
  Gulliver also opens Postgres sessions read-only, which stops writes but not functions such as
  `pg_terminate_backend`.
- **Reading allowed data.** Anything inside `allowed_paths` (by default the database file's folder and the
  current folder) can be read, except hidden files and folders. Keep secrets out of those folders, or set
  `allowed_paths` to the data folder only.
- **Slow or heavy reads** such as `pg_sleep` or a huge cross join are budget problems, not security ones: the
  runtime limit and the scan limits handle them.
- **Offline lint** (`gulliver lint` without a connection) checks function names and hidden paths, but not the
  folder allowlist, because it does not know the connection.

**What the human can change** (in `gulliver.toml`, per connection; the agent cannot):

```toml
[connections.lake]
engine = "duckdb"
path = "warehouse.duckdb"
allowed_paths = ["data", "/srv/lake"]   # replaces the default (database folder + current folder)
allow_remote = ["s3://my-bucket/"]      # or true for any URL; needs the httpfs extension installed
allow_functions = ["read_text"]         # lift a GS001-GS004 name check; path checks still apply
sandbox = true                          # false turns off the DuckDB engine sandbox (not recommended)
```
