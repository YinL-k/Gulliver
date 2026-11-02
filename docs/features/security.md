## A stronger read-only gate

**The problem.** "Only SELECT" is not the same as "read-only". A single SELECT can stop other people's sessions
(`pg_terminate_backend`), switch a read-only session back to read-write (`set_config('default_transaction_read_only', 'off', false)`),
run SQL hidden in a string that no parser sees (`dblink_exec`, `query_to_xml`, DuckDB `query()`, BigQuery
`EXTERNAL_QUERY`), or read files from the machine (`pg_read_file('/etc/passwd')`, DuckDB
`read_csv('~/.aws/credentials')`). Most database MCP servers check only the statement type, so all of these pass.

Gulliver now refuses them before anything reaches the database, with a code the agent can act on:

| Code | Name | Refuses |
|---|---|---|
| `GS001` | server-control | `pg_terminate_backend`, `pg_cancel_backend`, `pg_reload_conf`, `pg_switch_wal`, replication slots, `pg_stat_reset`, `pg_log_standby_snapshot`, `SYSTEM$CANCEL_QUERY`, `SYSTEM$ABORT_SESSION`, any Snowflake `SYSTEM$` function not known to be read-only |
| `GS002` | session-state | `set_config`, advisory locks, `pg_notify`, `nextval`/`setval` in a read (Snowflake `seq.NEXTVAL` too), large-object writes, index maintenance that writes (`brin_summarize_new_values`, `brin_summarize_range`, `gin_clean_pending_list`), `SELECT ... FOR UPDATE / FOR SHARE` |
| `GS003` | dynamic-sql | `dblink*`, `query_to_xml*`, `cursor_to_xml*`, `ts_stat`, `ts_rewrite`, DuckDB `query` / `query_table` / `postgres_query`, BigQuery `EXTERNAL_QUERY` |
| `GS004` | file-access | `pg_read_file`, `pg_ls_dir`, `lo_import`/`lo_export`, DuckDB `read_text`, `read_blob`, `getenv`, `duckdb_secrets`, `http_get`, `postgres_scan`; and any DuckDB file path or URL outside the allowed folders, a hidden file (`.env`, `.gulliver/`, also written as a glob such as `[.]env`, `?env`, `*env` or `**/credentials`), a computed path, `../` (also after a glob, as in `su?/../../x`) or a symlink that leads out. File readers include `read_duckdb`, `read_single_*_file` and `parquet_full_metadata` |

Names are matched after lower-casing and removing the schema, quotes and `U&"..."` escapes, everywhere in the
statement: CTEs, subqueries, `LATERAL`, UNION branches, and the SELECT part of a write.

**Globs.** A glob can match a hidden name without writing the dot (`[.]env`, `?env`, `*env`, `**/credentials`).
A glob name that starts with `?` or `[` is refused outright. For `*` and `**` Gulliver lists what the glob matches
right now (hidden names included, as DuckDB does) and refuses the query when any match is hidden, or resolves
outside the allowed folders. `..` after a glob character is refused.

**Default folders.** Without `allowed_paths`, the database folder and the current folder are allowed, except
when one of them is `/`, the home folder or a folder above it (MCP clients often start servers in `/` or in the
home folder). That folder is then left out; set `allowed_paths` to open it. `gulliver selftest security` fails
when an allowed folder is that broad.

**The DuckDB sandbox.** The parser is a denylist, so DuckDB is also locked at the engine. Every DuckDB connection
Gulliver opens runs:

```sql
SET allowed_directories = ['<database folder>/', '<current folder>/'];   -- or allowed_paths
SET enable_external_access = false;
SET autoinstall_known_extensions = false;
SET autoload_known_extensions = false;
SET lock_configuration = true;
```

So a file outside the allowed folders fails inside DuckDB itself, even for SQL the parser did not flag, and
the agent cannot `SET` it back. Parquet and CSV files in the database folder or the current folder keep working
as before.

### Settings (per connection, set by the human)

```toml
[connections.lake]
engine = "duckdb"
path = "warehouse.duckdb"
allowed_paths = ["data", "/srv/lake"]   # folders that may be read; default: the database folder + current folder (not /, ~ or above ~)
allow_remote = ["s3://my-bucket/"]      # or true for any URL (default false); httpfs must already be installed
allow_functions = ["read_text"]         # lift the name check for these functions; path checks still apply
sandbox = true                          # false turns off the engine sandbox (the parser still checks)
```

Relative paths resolve against the folder Gulliver runs in. `allow_functions` also works for Postgres,
BigQuery and Snowflake connections (for example `["nextval"]`).

### Example

```console
$ gulliver --dsn demo.duckdb preflight "select * from read_csv('/etc/passwd')" --json
{
  "status": "block",
  "statement": {
    "kind": "block",
    "code": "GS004",
    "reason": "GS004 file-access: read_csv() reads '/etc/passwd', which is outside the allowed folders (/home/me/project). A human can add a folder to allowed_paths on the connection in gulliver.toml."
  },
  "next_step": "GS004 file-access: read_csv() reads '/etc/passwd', ..."
}
```

### `gulliver selftest security`

Runs the bypass test set that ships with Gulliver (`src/gulliver/security_corpus.toml`, more than 130 cases:
known bypasses that must be refused with the right code, and ordinary SQL such as `date_trunc`, `coalesce`,
`regexp_*` and `read_parquet` on allowed paths that must stay allowed). Then it opens the connection (DuckDB; an
in-memory DuckDB for other engines) and checks the sandbox live: external access off, extension auto-load off,
settings locked, and a temporary file outside `allowed_paths` refused by DuckDB itself, past the parser.

```console
$ gulliver selftest security
Gulliver security selftest: ok
bypass test set: 132/132 cases as expected
DuckDB sandbox on 'default':
  ok      external access off: enable_external_access = false
  ok      extension autoload off: autoload_known_extensions and autoinstall_known_extensions
  ok      settings locked: SET is refused (lock_configuration)
  ok      file outside allowed_paths is refused by DuckDB: Permission Error: Cannot access file "/tmp/gulliver-selftest-.../outside.csv" - file system operations are disabled by configuration
```

Exit code 0 when everything passes, 4 when a case or a sandbox check fails (`status: fail`). `--json` prints
the full result; `--corpus FILE` runs your own cases in the same format.

### Guarantees and limits

- Every SQL text that reaches an engine through Gulliver is checked: preflight, rehearse, run, diff, check,
  incremental savings and translate `--check`. Blocks have `status: block` and `statement.code`.
- **It is a denylist plus a path allowlist, not a proof.** New functions and extensions appear. For DuckDB the
  engine sandbox does not depend on names; for Postgres, BigQuery and Snowflake the parser is Gulliver's only
  check, so **keep a read-only database role** for those.
- Functions and views defined in the database can call anything; Gulliver only sees the name you call.
- Files inside the allowed folders can be read (hidden ones excepted). Keep secrets out of them.
- Offline `gulliver lint` checks function names and hidden paths, not the folder allowlist (it has no connection).
- Not built: a Postgres `pg_proc` lookup that flags user functions in untrusted languages or with
  `SECURITY DEFINER`. It needs a live-server test setup that was not available; it is a good next step.

**Breaking change.** DuckDB connections can no longer read files outside the database folder and the current
folder, read remote URLs, or auto-load extensions, unless the human sets `allowed_paths` / `allow_remote` (or
`sandbox = false`). Queries that call the functions above are now refused; `allow_functions` lifts that per
connection.

## CHANGELOG

- **Security: stronger read-only gate.** SELECTs that stop sessions, change settings or locks, run SQL hidden in
  strings, or read files and URLs are refused with codes GS001-GS004, in every dialect. DuckDB connections are
  sandboxed in the engine (`allowed_directories`, no external access, no extension auto-load, locked settings).
  New per-connection settings `allowed_paths`, `allow_remote`, `allow_functions` and `sandbox`. New
  `gulliver selftest security` runs the published bypass test set (130+ cases) and checks the sandbox live.
  Threat model in SECURITY.md. **Breaking:** DuckDB can no longer read files outside the database folder and the
  current folder unless `allowed_paths` says so.

## AGENTS

Row for the tools table (CLI only, no MCP tool):

| (human) | `gulliver selftest security --json` | check the read-only gate itself: the bypass test set and the DuckDB sandbox (`fail` if a check fails) |

Bullet for "Rules you cannot change":

- SQL that calls a side-effect function, runs SQL from a string, or reads a file or URL outside the allowed
  folders is blocked with `statement.code` GS001-GS004. Rewrite it to read the tables directly; do not work
  around it. Only the human can change `allowed_paths`, `allow_remote` and `allow_functions`.
