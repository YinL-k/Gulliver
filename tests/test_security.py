"""The stronger read-only gate: side-effect functions, SQL in strings, file paths, and the DuckDB sandbox."""

from __future__ import annotations

import json
import os

import duckdb
import pytest

from gulliver import core, features, security
from gulliver.cli import main
from gulliver.config import ConfigError, Connection, load_settings
from gulliver.engines import EngineError
from gulliver.engines.duckdb_engine import DuckDBEngine
from gulliver.sqlsafety import classify

CASES = security.load_corpus()


# -- the bypass test set --------------------------------------------------------------------------
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_bypass_corpus(case):
    result = security.run_case(case)
    assert result["passed"], result


def test_corpus_shape():
    ids = [c["id"] for c in CASES]
    assert len(ids) >= 80 and len(ids) == len(set(ids))
    assert {c["expect"] for c in CASES} == {"read", "write", "block"}
    assert {c.get("code") for c in CASES if c.get("code")} == set(security.CODES)
    assert {c["dialect"] for c in CASES} == {"duckdb", "postgres", "bigquery", "snowflake"}
    assert sum(c["expect"] != "block" for c in CASES) >= 20  # false positives are tested too


def test_block_carries_code_and_label():
    s = classify("select pg_terminate_backend(1)", "postgres")
    assert s.kind == "block" and s.code == "GS001"
    assert s.reason.startswith("GS001 server-control:")
    assert s.public()["code"] == "GS001"
    assert "code" not in classify("select 1").public()


def test_offline_checks_names_but_not_folders():
    # Without a connection Gulliver cannot know the allowed folders: names and hidden paths are still checked.
    assert classify("select getenv('HOME')").code == "GS004"
    assert classify("select * from read_csv('.env')").code == "GS004"
    assert classify("select * from read_csv('/data/x.csv')").kind == "read"
    lint = core.lint_sql("select dblink_exec('h', 'drop table t')", "postgres")
    assert lint["status"] == "block" and lint["statement"]["code"] == "GS003"


# -- policy from the connection -------------------------------------------------------------------
def test_policy_defaults(tmp_path):
    db = tmp_path / "db" / "x.duckdb"
    conn = Connection("x", "duckdb", {"path": str(db)})
    policy = security.policy_for(conn, cwd=str(tmp_path / "work"))
    assert policy.allowed_paths == (str(tmp_path / "db"), str(tmp_path / "work"))
    assert policy.allow_remote is False and policy.allow_functions == frozenset()
    mem = security.policy_for(Connection("m", "duckdb", {"path": ":memory:"}), cwd=str(tmp_path))
    assert mem.allowed_paths == (str(tmp_path),)
    pg = security.policy_for(Connection("p", "postgres", {"dsn": "postgresql://x/y", "allow_functions": ["nextval"]}))
    assert pg.allowed_paths is None and pg.allow_functions == {"nextval"}


def test_policy_from_config_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GULLIVER_DSN", raising=False)
    (tmp_path / "gulliver.toml").write_text(
        "[connections.lake]\nengine = 'duckdb'\npath = ':memory:'\nallowed_paths = ['/srv/lake', 'data']\n"
        "allow_remote = ['s3://my-bucket/']\nallow_functions = ['READ_TEXT']\n", encoding="utf-8")
    conn = load_settings().connection("lake")
    policy = security.policy_for(conn)
    assert policy.allowed_paths == ("/srv/lake", str(tmp_path / "data"))
    assert policy.allow_remote == ("s3://my-bucket/",)
    assert policy.allow_functions == {"read_text"}
    assert classify("select * from read_parquet('/srv/lake/a/*.parquet')", "duckdb", policy).kind == "read"
    assert classify("select * from read_parquet('s3://my-bucket/a.parquet')", "duckdb", policy).kind == "read"
    assert classify("select * from read_parquet('/srv/other/a.parquet')", "duckdb", policy).code == "GS004"


def test_policy_from_dsn_query_and_bad_values():
    conn = Connection("d", "duckdb", {"path": ":memory:", "allow_remote": "true"})
    assert security.policy_for(conn).allow_remote is True
    with pytest.raises(ConfigError):
        security.policy_for(Connection("d", "duckdb", {"path": ":memory:", "allow_remote": "maybe"}))
    with pytest.raises(ConfigError):
        security.policy_for(Connection("d", "duckdb", {"path": ":memory:", "allowed_paths": [1, 2]}))


def test_symlink_out_of_allowed_folder_is_refused_by_the_parser(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("a\n1\n")
    (allowed / "link.csv").symlink_to(outside)
    policy = security.SecurityPolicy(allowed_paths=(str(allowed),), allow_remote=False, base_dir=str(allowed))
    assert classify("select * from read_csv('link.csv')", "duckdb", policy).code == "GS004"


# -- the DuckDB sandbox, at the engine (past the parser) -------------------------------------------
def _write_files(folder):
    folder.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"COPY (SELECT range AS id FROM range(5)) TO '{folder / 'x.parquet'}'")
    con.execute(f"COPY (SELECT range AS id FROM range(3)) TO '{folder / 'x.csv'}'")
    con.close()


def test_engine_refuses_files_outside_allowed_paths(tmp_path):
    inside, outside = tmp_path / "inside", tmp_path / "outside"
    _write_files(inside)
    _write_files(outside)
    engine = DuckDBEngine(Connection("s", "duckdb", {"path": ":memory:", "allowed_paths": [str(inside)]}))
    try:
        assert engine.execute(f"SELECT count(*) AS n FROM read_parquet('{inside / 'x.parquet'}')", 5).preview == [{"n": 5}]
        assert engine.execute(f"SELECT count(*) AS n FROM '{inside / 'x.csv'}'", 5).preview == [{"n": 3}]
        for sql in (f"SELECT * FROM read_csv('{outside / 'x.csv'}')", f"SELECT * FROM '{outside / 'x.parquet'}'",
                    f"SELECT * FROM read_csv('{inside}/../outside/x.csv')", "SELECT * FROM glob('/etc/*')",
                    "SELECT * FROM read_csv('https://example.com/x.csv')"):
            with pytest.raises(EngineError):
                engine.execute(sql, 5)
        with pytest.raises(duckdb.Error):
            engine.con.execute("SET enable_external_access = true")
        with pytest.raises(duckdb.Error):
            engine.con.execute("SET allowed_directories = ['/']")
    finally:
        engine.close()


def test_engine_refuses_symlink_escape(tmp_path):
    inside = tmp_path / "inside"
    _write_files(tmp_path / "outside")
    inside.mkdir()
    (inside / "link.csv").symlink_to(tmp_path / "outside" / "x.csv")
    engine = DuckDBEngine(Connection("s", "duckdb", {"path": ":memory:", "allowed_paths": [str(inside)]}))
    try:
        with pytest.raises(EngineError):
            engine.execute(f"SELECT * FROM read_csv('{inside / 'link.csv'}')", 5)
    finally:
        engine.close()


def test_sandbox_can_be_turned_off_by_the_human(tmp_path):
    _write_files(tmp_path / "outside")
    engine = DuckDBEngine(Connection("s", "duckdb", {"path": ":memory:", "allowed_paths": [str(tmp_path / "x")],
                                                      "sandbox": False}))
    try:
        assert engine.sandbox == []
        assert engine.execute(f"SELECT count(*) AS n FROM '{tmp_path / 'outside' / 'x.csv'}'", 1).preview == [{"n": 3}]
    finally:
        engine.close()


def test_two_engines_on_one_file_share_the_sandbox(demo_db):
    conn = Connection("d", "duckdb", {"path": demo_db})
    first = DuckDBEngine(conn)
    try:
        second = DuckDBEngine(conn)
        assert second.sandbox and second.execute("SELECT count(*) AS n FROM users", 1).rows == 1
        second.close()
    finally:
        first.close()


# -- end to end: preflight / run / internal queries -----------------------------------------------
def test_preflight_and_run_block_side_effects(settings):
    for sql, code in (("select getenv('HOME')", "GS004"), ("select * from query('select 1')", "GS003"),
                      ("select nextval('s')", "GS002"), ("select * from read_csv('/etc/passwd')", "GS004")):
        pre = core.preflight(settings, sql)
        assert pre["status"] == "block" and pre["statement"]["code"] == code, pre
        run = core.run(settings, sql)
        assert run["status"] == "block" and "preview" not in run
        assert core.rehearse(settings, sql)["status"] == "block"


def test_run_reads_parquet_in_the_working_folder(settings, state):
    folder = state.parent  # the state fixture runs the test in tmp_path
    _write_files(folder / "lake")
    sql = "select count(*) as n from read_parquet('lake/*.parquet')"
    assert core.preflight(settings, sql)["status"] in ("ok", "warn")
    out = core.run(settings, sql)
    assert out["status"] == "ok" and out["preview"] == [{"n": 5}]


def test_allowed_paths_lets_a_human_open_another_folder(demo_db, state, tmp_path_factory, monkeypatch):
    lake = tmp_path_factory.mktemp("lake")
    _write_files(lake)
    sql = f"select count(*) as n from '{lake / 'x.csv'}'"
    blocked = core.run(load_settings(dsn=demo_db), sql)
    assert blocked["status"] == "block" and blocked["statement"]["code"] == "GS004"
    (state.parent / "gulliver.toml").write_text(
        f"[connections.lake]\npath = '{demo_db}'\nengine = 'duckdb'\nallowed_paths = ['{lake}']\n", encoding="utf-8")
    out = core.run(load_settings(), sql)
    assert out["status"] == "ok" and out["preview"] == [{"n": 3}]


def test_internal_queries_use_the_connection_policy(settings):
    out = features.check(settings, "select * from read_csv('/etc/passwd')", min_rows=1)
    assert out["status"] == "block" and "GS004" in out["next_step"]
    # Gulliver's own queries still pass the gate
    assert features.check(settings, "users", unique=["user_id"])["status"] == "ok"


# -- gulliver selftest security --------------------------------------------------------------------
def test_cli_selftest_security(state, capsys):
    assert main(["selftest", "security", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["corpus"]["cases"] == len(CASES) and out["corpus"]["failed"] == []
    checks = {c["check"]: c["status"] for c in out["sandbox"]["checks"]}
    assert checks["settings locked"] == "ok" and checks["external access off"] == "ok"
    assert checks["file outside allowed_paths is refused by DuckDB"] in ("ok", "skipped")
    assert main(["selftest", "security"]) == 0
    assert "cases as expected" in capsys.readouterr().out


def test_selftest_reports_a_failing_case(state, tmp_path, capsys):
    bad = tmp_path / "corpus.toml"
    bad.write_text("[[case]]\nid = 'wrong'\ndialect = 'postgres'\nsql = 'select pg_terminate_backend(1)'\n"
                   "expect = 'read'\n", encoding="utf-8")
    assert main(["selftest", "security", "--corpus", str(bad)]) == 4
    assert "FAIL wrong" in capsys.readouterr().out


def test_selftest_flags_a_connection_without_sandbox(state, tmp_path):
    (tmp_path / "gulliver.toml").write_text("[connections.open]\nengine = 'duckdb'\npath = ':memory:'\n"
                                            "sandbox = false\n", encoding="utf-8")
    out = security.selftest(load_settings(), "open")
    assert out["status"] == "fail"
    assert {c["check"] for c in out["sandbox"]["checks"] if c["status"] == "fail"} >= {"sandbox on"}


# -- live Postgres (skipped without GULLIVER_TEST_PG_DSN) -----------------------------------------
PG_DSN = os.environ.get("GULLIVER_TEST_PG_DSN")


@pytest.mark.skipif(not PG_DSN, reason="set GULLIVER_TEST_PG_DSN to run live Postgres tests")
def test_postgres_side_effects_blocked_before_the_server(state):
    pg = load_settings(dsn=PG_DSN)
    for sql in ("select pg_terminate_backend(pg_backend_pid())", "select set_config('statement_timeout', '0', false)",
                "select pg_read_file('postgresql.conf')"):
        out = core.run(pg, sql)
        assert out["status"] == "block" and out["statement"]["code"] in security.CODES, out
    assert core.run(pg, "select current_setting('server_version_num') as v")["status"] == "ok"


def test_inspect_does_not_reveal_files_outside_allowed_paths(settings):
    out = core.inspect(settings, table="read_csv('/etc/passwd')")
    assert out["status"] == "ok"
    assert "bytes" not in out["table"] and "GS004" in out["table"]["notes"][0]
