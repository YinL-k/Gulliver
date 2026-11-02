"""The read-only gate, part two: side effects that hide inside a SELECT.

`sqlsafety.classify` decides what kind of statement the SQL is. A SELECT can still do harm through a function
call or a file path, so classify also asks `check()` here. Codes:

  GS001 server-control   stops sessions or changes the server (pg_terminate_backend, SYSTEM$CANCEL_QUERY, ...)
  GS002 session-state    changes settings, locks, sequences or large objects (set_config, nextval, FOR UPDATE)
  GS003 dynamic-sql      runs SQL text the parser never sees (dblink, query_to_xml, DuckDB query(), EXTERNAL_QUERY)
  GS004 file-access      reads server files, environment variables, other databases, or a path / URL outside
                         the folders the human allowed

This is a denylist plus a path allowlist. A denylist is never complete, so DuckDB is also sandboxed at the engine
(see `sandbox_duckdb`), and for warehouses a read-only database role stays the real boundary.

Per connection, set by the human in gulliver.toml:
  allowed_paths   = ["data", "/srv/lake"]   # DuckDB: folders that may be read (default: db file folder + cwd)
  allow_remote    = false                   # DuckDB: true, or a list of URL prefixes such as ["s3://my-bucket/"]
  allow_functions = ["nextval"]             # function names that may be called although they are listed here
  sandbox         = true                    # DuckDB: lock file access in the engine too (false turns it off)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlglot import exp

from .config import ConfigError, Connection

CODES = {
    "GS001": "server-control",
    "GS002": "session-state",
    "GS003": "dynamic-sql",
    "GS004": "file-access",
}

_WHY = {
    "GS001": "controls the server (it can stop other sessions, reload settings or change replication)",
    "GS002": "changes session or database state (settings, locks, sequences, notifications or large objects)",
    "GS003": "runs SQL or a remote query from a string, which Gulliver's parser cannot check",
    "GS004": "reads server files, environment variables, secrets or another database",
}


def _words(text: str) -> set[str]:
    return set(text.split())


# Exact names (lower case, schema stripped). Applied in every dialect: SQL moves between dialects
# (gulliver_translate), and none of these is an everyday function in any of them.
_FUNCTIONS: dict[str, str] = {}
for _code, _names in {
    "GS001": """pg_terminate_backend pg_cancel_backend pg_signal_backend pg_reload_conf pg_rotate_logfile
        pg_rotate_logfile_old pg_switch_wal pg_switch_xlog pg_promote pg_create_restore_point pg_backup_start
        pg_backup_stop pg_start_backup pg_stop_backup pg_wal_replay_pause pg_wal_replay_resume
        pg_xlog_replay_pause pg_xlog_replay_resume pg_create_physical_replication_slot
        pg_create_logical_replication_slot pg_drop_replication_slot pg_copy_physical_replication_slot
        pg_copy_logical_replication_slot pg_replication_slot_advance pg_logical_slot_get_changes
        pg_logical_slot_get_binary_changes pg_logical_emit_message pg_replication_origin_create
        pg_replication_origin_drop pg_replication_origin_advance pg_replication_origin_session_setup
        pg_replication_origin_session_reset pg_replication_origin_xact_setup pg_replication_origin_xact_reset
        pg_log_backend_memory_contexts pg_import_system_collations pg_stat_reset pg_stat_reset_shared
        pg_stat_reset_single_table_counters pg_stat_reset_single_function_counters pg_stat_reset_slru
        pg_stat_reset_replication_slot pg_stat_reset_subscription_stats pg_stat_statements_reset
        system$cancel_query system$cancel_all_queries system$abort_session system$abort_transaction""",
    "GS002": """set_config nextval setval pg_notify pg_advisory_lock pg_advisory_lock_shared pg_advisory_xact_lock
        pg_advisory_xact_lock_shared pg_try_advisory_lock pg_try_advisory_lock_shared pg_try_advisory_xact_lock
        pg_try_advisory_xact_lock_shared pg_advisory_unlock pg_advisory_unlock_shared pg_advisory_unlock_all
        lo_create lo_creat lo_unlink lo_put lo_from_bytea lo_truncate lowrite""",
    "GS003": """dblink dblink_exec dblink_open dblink_fetch dblink_close dblink_send_query dblink_get_result
        dblink_connect dblink_connect_u query_to_xml query_to_xmlschema query_to_xml_and_xmlschema cursor_to_xml
        cursor_to_xmlschema ts_stat pg_background_launch query query_table json_execute_serialized_sql
        postgres_query postgres_execute mysql_query mysql_execute external_query""",
    "GS004": """pg_read_file pg_read_binary_file pg_stat_file pg_ls_dir pg_ls_logdir pg_ls_waldir pg_ls_tmpdir
        pg_ls_archive_statusdir pg_ls_logicalsnapdir pg_ls_logicalmapdir pg_ls_replslotdir pg_file_write
        pg_file_sync pg_file_rename pg_file_unlink pg_logdir_ls lo_import lo_export read_text read_blob getenv
        duckdb_secrets which_secret http_get http_post http_put http_patch http_delete http_head http
        postgres_scan postgres_scan_pushdown postgres_attach mysql_scan sqlite_attach""",
}.items():
    for _name in _names.split():
        _FUNCTIONS[_name] = _code

# Sequences advance in a write that a human enabled (INSERT ... SELECT nextval(...)); in a read they are a side effect.
_WRITE_OK = {"nextval"}

# Snowflake SYSTEM$ functions: many change state, so only these known read-only ones pass.
_SNOWFLAKE_READ_ONLY = _words("""system$clustering_information system$clustering_depth system$clustering_ratio
    system$typeof system$explain_plan_json system$explain_json_to_text system$explain_json system$stream_has_data
    system$last_change_commit_time system$get_tag system$get_tag_allowed_values system$get_tag_on_current_column
    system$get_tag_on_current_table system$pipe_status system$estimate_search_optimization_costs
    system$estimate_query_acceleration system$allowlist system$allowlist_privatelink system$whitelist
    system$whitelist_privatelink system$get_privatelink_config system$current_user_task_name
    system$get_predecessor_return_value system$get_task_graph_config system$task_runtime_info""")

# DuckDB functions that read a file or URL given as the first argument: the path is checked, not the name.
FILE_FUNCTIONS = _words("""read_csv read_csv_auto read_tsv read_parquet parquet_scan parquet_metadata parquet_schema
    parquet_file_metadata parquet_kv_metadata parquet_bloom_probe read_json read_json_auto read_json_objects
    read_json_objects_auto read_ndjson read_ndjson_auto read_ndjson_objects sniff_csv glob delta_scan iceberg_scan
    iceberg_metadata iceberg_snapshots read_xlsx st_read st_read_meta read_avro sqlite_scan
    read_text read_blob""")  # the last two are refused by name, and path-checked when a human allows them

REMOTE_PREFIXES = ("http://", "https://", "s3://", "s3a://", "s3n://", "gs://", "gcs://", "r2://", "az://",
                   "azure://", "abfss://", "hf://")

_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_FILE_LIKE = re.compile(r"\.(parquet|csv|tsv|json|jsonl|ndjson|txt|gz|zst|xlsx|arrow|avro|duckdb|db|sqlite)$", re.I)
_UNICODE_ESCAPE = re.compile(r"\\\+([0-9a-fA-F]{6})|\\([0-9a-fA-F]{4})")


@dataclass(frozen=True)
class SecurityPolicy:
    """What a connection may touch. `None` means "not known here" (offline lint): that check is skipped."""

    allowed_paths: tuple[str, ...] | None = None
    allow_remote: bool | tuple[str, ...] | None = None
    allow_functions: frozenset[str] = field(default_factory=frozenset)
    base_dir: str | None = None   # where relative paths resolve (default: the current directory)

    def public(self) -> dict[str, Any]:
        return {"allowed_paths": list(self.allowed_paths) if self.allowed_paths is not None else None,
                "allow_remote": list(self.allow_remote) if isinstance(self.allow_remote, tuple) else self.allow_remote,
                "allow_functions": sorted(self.allow_functions)}


OFFLINE = SecurityPolicy()


@dataclass(frozen=True)
class Violation:
    code: str
    reason: str
    target: str = ""     # the function name or path that was refused

    @property
    def label(self) -> str:
        return f"{self.code} {CODES[self.code]}"


# -- policy from the connection --------------------------------------------------------------
def _as_list(value: Any, key: str, conn_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return [v.strip() for v in value if v.strip()]
    raise ConfigError(f"Connection '{conn_name}': {key} must be a list of strings.")


def _as_bool(value: Any, key: str, conn_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "false", "0", "no"):
        return value.strip().lower() in ("true", "1", "yes")
    raise ConfigError(f"Connection '{conn_name}': {key} must be true or false.")


def allowed_roots(conn: Connection, cwd: str | None = None) -> list[str]:
    """Absolute folders a DuckDB connection may read: allowed_paths, or the database file's folder + cwd."""
    base = cwd or os.getcwd()
    configured = _as_list(conn.params.get("allowed_paths"), "allowed_paths", conn.name)
    if configured:
        roots = [os.path.abspath(os.path.join(base, os.path.expanduser(p))) for p in configured]
    else:
        roots = [base]
        path = str(conn.params.get("path") or ":memory:")
        if path != ":memory:":
            roots.insert(0, os.path.dirname(os.path.abspath(os.path.join(base, os.path.expanduser(path)))))
    out: list[str] = []
    for root in roots:
        if root not in out:
            out.append(root)
    return out


def _remote_setting(conn: Connection) -> bool | tuple[str, ...]:
    value = conn.params.get("allow_remote", False)
    if isinstance(value, (list, tuple)):
        return tuple(p.lower() for p in _as_list(value, "allow_remote", conn.name))
    if isinstance(value, str) and "://" in value:
        return tuple(p.lower() for p in _as_list(value, "allow_remote", conn.name))
    return _as_bool(value, "allow_remote", conn.name)


def policy_for(conn: Connection, cwd: str | None = None) -> SecurityPolicy:
    allow = frozenset(n.lower() for n in _as_list(conn.params.get("allow_functions"), "allow_functions", conn.name))
    if conn.engine != "duckdb":  # files and URLs only exist as table sources in DuckDB
        return SecurityPolicy(allow_functions=allow)
    return SecurityPolicy(allowed_paths=tuple(allowed_roots(conn, cwd)), allow_remote=_remote_setting(conn),
                          allow_functions=allow, base_dir=cwd or os.getcwd())


# -- the check ---------------------------------------------------------------------------------
def _decode(name: str) -> str:
    """Lower case, with Postgres U&"..." escapes (\\0041, \\+000041) decoded, so they cannot hide a name."""
    def repl(m: re.Match[str]) -> str:
        try:
            return chr(int(m.group(1) or m.group(2), 16))
        except ValueError:  # pragma: no cover - out of range code point
            return m.group(0)
    return _UNICODE_ESCAPE.sub(repl, name).strip().lower()


_HEADS: dict[tuple[type, str], str] = {}


def function_names(node: exp.Func, dialect: str) -> set[str]:
    """Every name a function call can go by: as written (Anonymous), sqlglot's name, and the dialect's name."""
    names: set[str] = set()
    if isinstance(node, exp.Anonymous):
        names.add(_decode(str(node.name)))
    else:
        names.add(node.sql_name().lower())
        names.add(type(node).__name__.lower())
        key = (type(node), dialect)
        if key not in _HEADS:  # the dialect's spelling, once per function type (generating SQL is not free)
            try:
                _HEADS[key] = _decode(node.sql(dialect=dialect).split("(", 1)[0].strip('"`[] '))
            except Exception:  # pragma: no cover - a generation failure must not open the gate
                _HEADS[key] = ""
        names.add(_HEADS[key])
    names.discard("")
    return {n.rsplit(".", 1)[-1] for n in names}


def _positional(node: exp.Func) -> list[exp.Expression]:
    args: list[exp.Expression] = []
    if not isinstance(node, exp.Anonymous) and isinstance(node.args.get("this"), exp.Expression):
        args.append(node.args["this"])
    args.extend(node.expressions)
    return [a for a in args if not isinstance(a, (exp.EQ, exp.PropertyEQ, exp.Kwarg))]


def _path_literals(arg: exp.Expression) -> list[str] | None:
    """The string paths in a file argument, or None when the path is computed (cannot be checked)."""
    if isinstance(arg, exp.Literal) and arg.is_string:
        return [arg.this]
    if isinstance(arg, (exp.Array, exp.Tuple)) or type(arg).__name__ == "List":
        out: list[str] = []
        for item in arg.expressions:
            got = _path_literals(item)
            if got is None:
                return None
            out.extend(got)
        return out
    return None


def looks_like_path(name: str) -> bool:
    return bool(name) and ("/" in name or "\\" in name or "://" in name or name.startswith("~")
                           or any(ch in name for ch in "*?") or bool(_FILE_LIKE.search(name)))


def _hidden_part(parts: list[str]) -> str | None:
    for part in parts:
        if part.startswith(".") and part not in (".", ".."):
            return part
    return None


def check_path(raw: str, policy: SecurityPolicy) -> Violation | None:
    text = raw.strip()
    if text.lower().startswith("file://"):
        text = text[len("file://"):]
    if _SCHEME.match(text):
        if policy.allow_remote is None or policy.allow_remote is True:
            return None
        if isinstance(policy.allow_remote, tuple) and text.lower().startswith(policy.allow_remote):
            return None
        allowed = ", ".join(policy.allow_remote) if isinstance(policy.allow_remote, tuple) else "none"
        return Violation("GS004", f"reads the remote URL '{raw}' (allowed remote prefixes: {allowed}). "
                                  "A human can set allow_remote on the connection in gulliver.toml.", raw)
    expanded = os.path.expanduser(text)
    if policy.allowed_paths is None:
        hidden = _hidden_part(re.split(r"[\\/]", expanded))
        if hidden:
            return Violation("GS004", f"reads the hidden path '{raw}' ({hidden}); hidden files often hold "
                                      "credentials or Gulliver's own state.", raw)
        return None
    base = policy.base_dir or os.getcwd()
    cut = min([i for i in (expanded.find(ch) for ch in "*?[{") if i >= 0], default=-1)
    fixed = expanded if cut < 0 else os.path.dirname(expanded[:cut])
    full = os.path.realpath(os.path.join(base, fixed or "."))
    for root in policy.allowed_paths:
        real_root = os.path.realpath(os.path.join(base, root))
        if full == real_root or full.startswith(real_root.rstrip(os.sep) + os.sep):
            rel_parts = list(Path(os.path.relpath(full, real_root)).parts)
            tail = re.split(r"[\\/]", expanded[len(fixed):]) if cut >= 0 else []  # the glob part, as written
            hidden = _hidden_part(rel_parts + tail)
            if hidden:
                return Violation("GS004", f"reads the hidden path '{raw}' ({hidden}); hidden files often hold "
                                          "credentials or Gulliver's own state.", raw)
            return None
    return Violation("GS004", f"reads '{raw}', which is outside the allowed folders "
                              f"({', '.join(policy.allowed_paths) or 'none'}). A human can add a folder to "
                              "allowed_paths on the connection in gulliver.toml.", raw)


def _function_violation(name: str, kind: str, policy: SecurityPolicy) -> Violation | None:
    """allow_functions lifts only this name check; the path check on file functions always applies."""
    if name in policy.allow_functions:
        return None
    code = _FUNCTIONS.get(name)
    if code is None and name.startswith("dblink"):
        code = "GS003"
    if code is None and name.startswith("pg_ls_"):
        code = "GS004"
    if code is None and name.startswith("system$") and name not in _SNOWFLAKE_READ_ONLY:
        return Violation("GS001", f"{name.upper()}() is a Snowflake system function that Gulliver does not know "
                                  "to be read-only (many cancel queries, abort sessions or change settings). "
                                  "A human can allow it with allow_functions in gulliver.toml.", name)
    if code is None or (kind == "write" and name in _WRITE_OK):
        return None
    return Violation(code, f"{name}() {_WHY[code]}. Gulliver only reads; query the tables directly. "
                           "A human can allow a function with allow_functions in gulliver.toml.", name)


def check(tree: exp.Expression, dialect: str, policy: SecurityPolicy | None = None,
          kind: str = "read") -> Violation | None:
    """The first side effect found in a parsed statement, or None."""
    policy = policy or OFFLINE
    duck = dialect == "duckdb"
    for node in tree.walk():
        if isinstance(node, exp.Lock) and kind == "read":
            return Violation("GS002", "SELECT ... FOR UPDATE / FOR SHARE locks rows that other sessions wait on. "
                                      "Remove the locking clause.", "FOR UPDATE")
        if isinstance(node, exp.Func):
            names = function_names(node, dialect)
            for name in sorted(names) if not names & policy.allow_functions else ():
                hit = _function_violation(name, kind, policy)
                if hit:
                    return hit
            if duck and names & FILE_FUNCTIONS:
                hit = _check_file_call(node, names, policy)
                if hit:
                    return hit
        elif (dialect == "snowflake" and isinstance(node, exp.Column) and node.name.lower() == "nextval"
              and node.table and kind == "read" and "nextval" not in policy.allow_functions):
            return Violation("GS002", f"{node.sql(dialect=dialect)} advances a sequence (Snowflake seq.NEXTVAL), "
                                      "so it is not a read.", "nextval")
        elif duck and isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier):
            name = str(node.this.this)
            if looks_like_path(name):
                hit = check_path(name, policy)
                if hit:
                    return hit
    return None


def _check_file_call(node: exp.Func, names: set[str], policy: SecurityPolicy) -> Violation | None:
    fname = sorted(names & FILE_FUNCTIONS)[0]
    args = _positional(node)
    if not args:
        return None
    paths = _path_literals(args[0])
    if paths is None:
        return Violation("GS004", f"{fname}() gets a computed path, so Gulliver cannot check which file it reads. "
                                  "Write the path as a plain string.", fname)
    for path in paths:
        hit = check_path(path, policy)
        if hit:
            return Violation("GS004", f"{fname}() " + hit.reason, hit.target)
    return None


# -- the DuckDB engine sandbox -----------------------------------------------------------------
def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def sandbox_duckdb(con: Any, conn: Connection) -> list[str]:
    """Lock a DuckDB connection to the allowed folders. Returns the settings applied ([] when turned off).

    After this, file and network access outside allowed_directories fails inside DuckDB itself, even for SQL the
    parser did not flag, and the agent cannot SET it back (lock_configuration)."""
    if not _as_bool(conn.params.get("sandbox", True), "sandbox", conn.name):
        return []
    # DuckDB shares one database instance per file inside a process: a second connection to the same file sees
    # the settings (and the lock) of the first one.
    locked, external = con.execute("SELECT current_setting('lock_configuration'), "
                                   "current_setting('enable_external_access')").fetchone()
    if str(locked).lower() == "true":
        if str(external).lower() != "false":
            raise ConfigError(f"Connection '{conn.name}': this DuckDB database is already open with its settings "
                              "locked but without the file sandbox; open it with one connection setting.")
        return ["(already sandboxed by an earlier connection to the same database)"]
    dirs: list[str] = []
    for root in allowed_roots(conn):
        for variant in (root, os.path.realpath(root)):
            entry = variant.rstrip(os.sep) + os.sep
            if entry not in dirs:
                dirs.append(entry)
    remote = _remote_setting(conn)
    if remote:
        try:  # remote files need httpfs; it is never installed or auto-loaded by Gulliver
            con.execute("LOAD httpfs")
        except Exception:
            pass
        dirs.extend(remote if isinstance(remote, tuple) else REMOTE_PREFIXES)
    statements = [
        "SET allowed_directories = [" + ", ".join(_quote(d) for d in dirs) + "]",
        "SET enable_external_access = false",
        "SET autoinstall_known_extensions = false",
        "SET autoload_known_extensions = false",
        "SET lock_configuration = true",
    ]
    for statement in statements:
        con.execute(statement)
    return statements


# -- selftest ------------------------------------------------------------------------------------
SELFTEST_ROOT = "/gulliver-selftest/data"


def corpus_path() -> Path:
    return Path(__file__).with_name("security_corpus.toml")


def load_corpus(path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    from .config import tomllib

    with open(path or corpus_path(), "rb") as fh:
        return list(tomllib.load(fh).get("case") or [])


def case_policy(case: dict[str, Any]) -> SecurityPolicy:
    """The fixed policy a corpus case runs under (it does not depend on the current folder)."""
    remote = case.get("allow_remote", False)
    return SecurityPolicy(allowed_paths=(SELFTEST_ROOT,), base_dir=SELFTEST_ROOT,
                          allow_remote=tuple(remote) if isinstance(remote, list) else bool(remote),
                          allow_functions=frozenset(n.lower() for n in case.get("allow_functions") or []))


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    from .sqlsafety import classify

    dialect = case.get("dialect", "duckdb")
    statement = classify(case["sql"], dialect, case_policy(case))
    expected_code = case.get("code", "")
    passed = statement.kind == case["expect"] and (not expected_code or statement.code == expected_code)
    out: dict[str, Any] = {"id": case["id"], "dialect": dialect, "passed": passed,
                           "expected": case["expect"], "got": statement.kind}
    if expected_code or statement.code:
        out.update(expected_code=expected_code, got_code=statement.code)
    if not passed:
        out.update(sql=case["sql"], reason=statement.reason)
    return out


def _sandbox_checks(conn: Connection) -> list[dict[str, Any]]:
    import tempfile

    from .engines import EngineError
    from .engines.duckdb_engine import DuckDBEngine

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool | None, detail: str) -> None:
        checks.append({"check": name, "status": "skipped" if ok is None else ("ok" if ok else "fail"),
                       "detail": detail})

    engine = DuckDBEngine(conn, read_only=True)
    try:
        def setting(name: str) -> str:
            return str(engine.con.execute(f"SELECT current_setting('{name}')").fetchone()[0]).lower()

        if not engine.sandbox:
            add("sandbox on", False, "sandbox = false on this connection: DuckDB itself does not limit file access")
        add("external access off", setting("enable_external_access") == "false",
            "enable_external_access = " + setting("enable_external_access"))
        add("extension autoload off",
            setting("autoload_known_extensions") == "false" and setting("autoinstall_known_extensions") == "false",
            "autoload_known_extensions and autoinstall_known_extensions")
        try:
            engine.con.execute("SET enable_external_access = true")
            add("settings locked", False, "SET enable_external_access = true was accepted")
        except Exception:
            add("settings locked", True, "SET is refused (lock_configuration)")
        name = "file outside allowed_paths is refused by DuckDB"
        with tempfile.TemporaryDirectory(prefix="gulliver-selftest-") as tmp:
            outside = os.path.join(tmp, "outside.csv")
            with open(outside, "w", encoding="utf-8") as fh:
                fh.write("secret\n42\n")
            real = os.path.realpath(outside)
            roots = [os.path.realpath(r) for r in allowed_roots(conn)]
            if any(real.startswith(r.rstrip(os.sep) + os.sep) for r in roots):
                add(name, None, f"the temp folder {tmp} is inside allowed_paths, so it cannot test this")
            else:
                try:  # straight to the engine, past the parser: DuckDB must refuse on its own
                    engine.execute(f"SELECT * FROM read_csv({_quote(outside)})", preview_rows=1)
                    add(name, False, f"{outside} was read")
                except EngineError as exc:
                    add(name, True, str(exc)[:160])
    finally:
        engine.close()
    return checks


def selftest(settings: Any = None, connection: str | None = None,
             corpus: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Run the bypass test set through the gate, then check the DuckDB sandbox on a live connection."""
    try:
        cases = load_corpus(corpus)
    except (OSError, ValueError) as exc:
        return {"status": "error", "error": f"Could not read the test set: {exc}",
                "next_step": "Reinstall Gulliver; the test set ships inside the package."}
    import logging

    sqlglot_log = logging.getLogger("sqlglot")
    level = sqlglot_log.level
    sqlglot_log.setLevel(logging.ERROR)  # "falling back to Command" warnings for the cases that must block
    try:
        results = [run_case(c) for c in cases]
    finally:
        sqlglot_log.setLevel(level)
    failed = [r for r in results if not r["passed"]]
    out: dict[str, Any] = {
        "suite": "security",
        "corpus": {"path": str(corpus or corpus_path()), "cases": len(results),
                   "passed": len(results) - len(failed), "failed": failed},
    }
    conn = settings.connection(connection) if settings is not None else None
    label = ""
    if conn is None or conn.engine != "duckdb":
        if conn is not None:
            label = f" (connection '{conn.name}' is {conn.engine}; an in-memory DuckDB was checked instead)"
        conn = Connection("selftest", "duckdb", {"path": ":memory:"})
    try:
        checks = _sandbox_checks(conn)
    except Exception as exc:  # the engine could not open: report it, do not hide it
        return {**out, "status": "error", "error": f"Could not open DuckDB connection '{conn.name}': {exc}",
                "next_step": "Fix the connection in gulliver.toml and run the selftest again."}
    out["sandbox"] = {"connection": conn.name + label, "allowed_paths": allowed_roots(conn), "checks": checks}
    bad = [c for c in checks if c["status"] == "fail"]
    out["status"] = "fail" if failed or bad else "ok"
    out["next_step"] = (
        "The gate behaved as the test set expects. It is a denylist plus a sandbox, not a proof: keep a read-only "
        "database role for warehouses." if out["status"] == "ok" else
        "Some checks failed (see corpus.failed and sandbox.checks). Do not rely on the gate until they pass, and "
        "please report a bypass privately (see SECURITY.md).")
    return out


def render_selftest(result: dict[str, Any]) -> str:
    corpus = result.get("corpus") or {}
    lines = [f"Gulliver security selftest: {result.get('status')}"]
    if corpus:
        lines.append(f"bypass test set: {corpus.get('passed')}/{corpus.get('cases')} cases as expected")
    for case in corpus.get("failed") or []:
        lines.append(f"  FAIL {case['id']} [{case['dialect']}]: expected {case['expected']} "
                     f"{case.get('expected_code', '')}, got {case['got']} {case.get('got_code', '')}".rstrip())
    sandbox = result.get("sandbox")
    if sandbox:
        lines.append(f"DuckDB sandbox on '{sandbox['connection']}':")
        for check in sandbox["checks"]:
            lines.append(f"  {check['status']:7} {check['check']}: {check['detail']}")
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    lines.append(f"\nnext: {result.get('next_step', '')}")
    return "\n".join(lines)
