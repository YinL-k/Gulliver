"""Runaway guard: loops, per-agent and per-session budgets, spend velocity, query tags, spend report, audit chain."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import duckdb
import pytest

from gulliver import audit, core, features, runaway
from gulliver.cli import main
from gulliver.config import ConfigError, Connection, load_settings
from gulliver.engines.base import EXACT, Estimate
from gulliver.engines.bigquery import BigQueryEngine, job_labels
from gulliver.engines.duckdb_engine import DuckDBEngine
from gulliver.engines.postgres import application_name
from gulliver.engines.snowflake import SnowflakeEngine
from gulliver.identity import Identity, from_env, use

Q = "select country, count(*) as n from users group by 1"


@pytest.fixture
def loopy(monkeypatch):
    """Treat every query as expensive enough to count as a loop (the demo data is tiny)."""
    monkeypatch.setattr(runaway, "REPEAT_FREE_BYTES", -1)


@pytest.fixture
def small_db(tmp_path):
    path = tmp_path / "small.duckdb"
    con = duckdb.connect(str(path))
    con.execute("create table users as select i as user_id, ['NL','BE','DE'][1 + i % 3] as country "
                "from range(3000) t(i)")
    con.close()
    return str(path)


class FixedCostDuckDB(DuckDBEngine):
    """DuckDB pretending to be a warehouse where every query costs $0.30."""

    billed = True
    PRICE = 0.30

    def estimate(self, sql):
        est = super().estimate(sql)
        return Estimate(bytes=est.bytes, rows=est.rows, cost_usd=self.PRICE, accuracy=EXACT, source="fake")

    def cost_for(self, bytes_processed, runtime_s):
        return self.PRICE

    def execute(self, sql, preview_rows, timeout_s=None, max_bytes_billed=None):
        res = super().execute(sql, preview_rows, timeout_s, max_bytes_billed)
        res.cost_usd = self.PRICE if sql.strip().upper() != "SELECT 1" else 0.0
        return res


@pytest.fixture
def billed(monkeypatch, demo_db, state):
    monkeypatch.setattr(core, "get_engine", lambda conn, read_only=True: FixedCostDuckDB(conn, read_only))

    def make(**budget):
        base = {"max_query_usd": 1.0, "approval_over_usd": 1.0, "rehearse_over_usd": 10.0, "day_usd": 100.0,
                "velocity_usd": 0, "max_repeats": 0}
        return load_settings(dsn=demo_db, budget_overrides={**base, **budget})

    return make


def ledger(settings):
    return [json.loads(line) for line in (settings.state_dir / "ledger.jsonl").read_text().splitlines()]


# -- repeats and the loop guard ---------------------------------------------------------------
def test_identical_runs_are_blocked_after_max_repeats(small_db, state, loopy):
    s = load_settings(dsn=small_db, budget_overrides={"max_repeats": 3})
    for _ in range(3):
        assert core.run(s, Q)["status"] == "ok"
    fourth = core.run(s, Q)
    assert fourth["status"] == "block" and fourth["reason"] == "loop"
    assert fourth["cached"]["cached"] is True
    assert {r["country"]: r["n"] for r in fourth["cached"]["preview"]} == {"NL": 1000, "BE": 1000, "DE": 1000}
    assert "cached.preview" in fourth["next_step"]
    assert len(ledger(s)) == 3                                   # the blocked run cost nothing
    other = core.run(s, Q.replace("count(*)", "count(user_id)"))  # a different query is fine
    assert other["status"] == "ok"


def test_writing_to_the_duckdb_file_lifts_the_loop_block(small_db, state, loopy):
    s = load_settings(dsn=small_db, budget_overrides={"max_repeats": 2})
    core.run(s, Q)
    core.run(s, Q)
    assert core.run(s, Q)["status"] == "block"
    con = duckdb.connect(small_db)
    con.execute("insert into users values (99999, 'FR')")
    con.close()
    after = core.run(s, Q)
    assert after["status"] == "ok"
    assert "tables changed" in after["runaway"]["note"]
    assert {r["country"] for r in after["preview"]} == {"NL", "BE", "DE", "FR"}
    versions = [e.get("data_version") for e in ledger(s)]
    assert versions[0] == versions[1] != versions[2]


def test_cheap_repeats_are_not_blocked(small_db, state):
    s = load_settings(dsn=small_db, budget_overrides={"max_repeats": 1})
    for _ in range(3):
        res = core.run(s, Q)
        assert res["status"] == "ok"
    assert "cheap" in res["runaway"]["note"]


def test_no_change_signal_asks_a_human(small_db, state, loopy):
    s = load_settings(dsn=small_db, budget_overrides={"max_repeats": 1})
    sql = "select count(*) as n from users where current_date > date '2000-01-01'"
    assert core.run(s, sql)["status"] == "ok"
    second = core.run(s, sql)
    assert second["status"] == "needs_approval" and second["reason"] == "repeat"
    assert f"gulliver approve {second['receipt_id']}" in second["next_step"]
    core.approve(s, second["receipt_id"])
    assert core.run(s, sql)["status"] == "ok"                  # the approval buys another max_repeats runs
    assert core.run(s, sql)["status"] == "needs_approval"


def test_in_memory_duckdb_has_no_change_signal(state):
    eng = DuckDBEngine(Connection("m", "duckdb", {"path": ":memory:"}))
    assert eng.data_version(["t"]) is None
    assert runaway.deterministic(None) is False


def test_views_and_nondeterministic_queries_have_no_change_signal(small_db):
    con = duckdb.connect(small_db)
    con.execute("create view v as select * from users")
    con.close()
    eng = DuckDBEngine(Connection("f", "duckdb", {"path": small_db}))
    try:
        assert eng.data_version(["users"]) is not None
        assert eng.data_version(["v"]) is None
        assert runaway.data_version(eng, "select * from users") is not None
        assert runaway.data_version(eng, "select random() from users") is None
        assert runaway.data_version(eng, "select now() from users") is None
    finally:
        eng.close()


def test_parquet_file_change_is_seen(tmp_path):
    pq = tmp_path / "x.parquet"
    duckdb.sql(f"copy (select 1 as a) to '{pq}' (format parquet)")
    eng = DuckDBEngine(Connection("f", "duckdb", {"path": str(tmp_path / "f.duckdb")}), read_only=False)
    try:
        before = eng.data_version([f"read_parquet('{pq}')"])
        duckdb.sql(f"copy (select 1 as a union all select 2) to '{pq}' (format parquet)")
        after = eng.data_version([f"read_parquet('{pq}')"])
        assert before and after and before != after
        assert eng.data_version(["read_parquet('s3://bucket/x.parquet')"]) is None
    finally:
        eng.close()


def test_internal_queries_are_guarded_too(small_db, state, loopy):
    s = load_settings(dsn=small_db, budget_overrides={"max_repeats": 1})
    assert features.check(s, "users", unique=["user_id"])["status"] == "ok"
    again = features.check(s, "users", unique=["user_id"])
    assert again["status"] == "block"
    assert "Loop" in again["next_step"]


# -- budgets per agent and session, velocity ------------------------------------------------------
def test_agent_day_budget_per_agent(billed):
    s = billed(agent_day_usd={"default": 0.5, "big": 2.0})
    with use(Identity("small")):
        assert core.run(s, Q)["status"] == "ok"
        res = core.run(s, Q + " order by 1")
        assert res["status"] == "block" and res["reason"] == "agent_budget"
        assert "small" in res["next_step"]
    with use(Identity("big")):
        assert core.run(s, Q)["status"] == "ok"
        assert core.run(s, Q + " order by 1")["status"] == "ok"
    assert {e["agent"] for e in ledger(s)} == {"small", "big"}


def test_session_budget(billed, monkeypatch):
    monkeypatch.setenv("GULLIVER_SESSION", "s1")
    s = billed(session_usd=0.5)
    assert core.run(s, Q)["status"] == "ok"
    res = core.run(s, Q + " order by 1")
    assert res["status"] == "block" and res["reason"] == "session_budget"
    monkeypatch.setenv("GULLIVER_SESSION", "s2")
    assert core.run(s, Q + " order by 1")["status"] == "ok"
    status = core.status(s)["caller"]
    assert status["session"] == "s2" and status["session_spent_usd"] == pytest.approx(0.3)


def test_spend_velocity_needs_approval(billed):
    s = billed(velocity_usd=0.5)
    assert core.run(s, Q)["status"] == "ok"
    res = core.run(s, Q + " order by 1")
    assert res["status"] == "needs_approval" and res["reason"] == "velocity"
    core.approve(s, res["receipt_id"])
    assert core.run(s, Q + " order by 1")["status"] == "ok"


def test_rehearsal_respects_agent_budget(billed, monkeypatch):
    s = billed(agent_day_usd=0.3)
    assert core.run(s, Q)["status"] == "ok"
    monkeypatch.setattr(core, "SMALL_DATA_ROWS", 10)
    res = core.rehearse(s, Q)
    assert res["status"] == "block" and res["reason"] == "agent_budget"


def test_mcp_clients_get_their_own_agent_budget(billed, monkeypatch, tmp_path):
    from mcp import Client
    from mcp.types import Implementation

    from gulliver.mcp_server import build_server

    monkeypatch.setenv("GULLIVER_DSN", billed().connection().params["path"])
    (tmp_path / "gulliver.toml").write_text(
        "[budget]\nmax_query_usd = 1.0\napproval_over_usd = 1.0\nrehearse_over_usd = 10.0\nday_usd = 100.0\n"
        "velocity_usd = 0\nmax_repeats = 0\nagent_day_usd = { default = 0.5, \"agent-b\" = 5 }\n")
    server = build_server()

    async def two_runs(name):
        async with Client(server, client_info=Implementation(name=name, version="1")) as client:
            out = []
            for sql in (Q, Q + " order by 1"):
                result = await client.call_tool("gulliver_run", {"sql": sql})
                out.append(json.loads(result.content[0].text))
            status = json.loads((await client.call_tool("gulliver_status", {})).content[0].text)
            return out, status

    a, a_status = asyncio.run(two_runs("agent-a"))
    b, _ = asyncio.run(two_runs("agent-b"))
    assert [r["status"] for r in a] == ["ok", "block"] and a[1]["reason"] == "agent_budget"
    assert [r["status"] for r in b] == ["ok", "ok"]
    assert a_status["caller"]["agent"] == "agent-a" and a_status["caller"]["agent_day_limit_usd"] == 0.5
    entries = [json.loads(x) for x in (tmp_path / "state" / "ledger.jsonl").read_text().splitlines()]
    assert {e["agent"] for e in entries} == {"agent-a", "agent-b"}
    assert len({e["session"] for e in entries}) == 2 and all(e["session"].startswith("mcp-") for e in entries)


def test_human_set_agent_wins_over_client_name(monkeypatch):
    monkeypatch.setenv("GULLIVER_AGENT", "nightly-job")
    monkeypatch.setenv("GULLIVER_TASK", "fix the dashboard")
    ident = from_env("whatever")
    assert ident.agent == "nightly-job" and ident.source == "env" and ident.task == "fix-the-dashboard"
    monkeypatch.delenv("GULLIVER_AGENT")
    assert from_env("a b*/").agent == "a-b"


# -- query tags ----------------------------------------------------------------------------------------
def test_tags_carry_identity_and_receipt(small_db, state):
    s = load_settings(dsn=small_db)
    seen = {}
    original = DuckDBEngine.execute

    def spy(self, sql, *args, **kwargs):
        seen.update(self.tags)
        return original(self, sql, *args, **kwargs)

    with use(Identity("tagger", session="s9", task="t1")):
        DuckDBEngine.execute = spy
        try:
            res = core.run(s, Q)
        finally:
            DuckDBEngine.execute = original
    assert seen == {"tool": "gulliver", "kind": "run", "agent": "tagger", "session": "s9", "task": "t1",
                    "receipt": res["receipt_id"]}


class FakeBQClient:
    def __init__(self):
        self.calls = []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return SimpleNamespace(total_bytes_processed=1, total_bytes_billed=1, referenced_tables=[],
                               result=lambda timeout=None, page_size=None: [])

    def get_table(self, ref):
        if ref.endswith("_view"):
            return SimpleNamespace(table_type="VIEW", modified=datetime(2026, 1, 1, tzinfo=timezone.utc))
        return SimpleNamespace(table_type="TABLE", modified=datetime(2026, 1, 1, tzinfo=timezone.utc), num_rows=5,
                               num_bytes=50, streaming_buffer=None)


def test_bigquery_job_labels_and_change_signal():
    client = FakeBQClient()
    eng = BigQueryEngine(Connection("bq", "bigquery", {"project": "p"}), _client=client)
    eng.tags = {"tool": "gulliver", "kind": "run", "agent": "Claude Code", "receipt": "gv-20261102-run-ab12"}
    eng.execute("select 1", preview_rows=1)
    assert client.calls[-1][1]["labels"] == {"gulliver": "run", "gulliver_agent": "claude-code",
                                             "gulliver_receipt": "gv-20261102-run-ab12"}
    assert eng.data_version(["ds.events"]).startswith("bigquery:ds.events:2026-01-01")
    assert eng.data_version(["ds.events_view"]) is None
    assert all(len(v) <= 63 for v in job_labels({"kind": "x" * 100}).values())


class FakeSnowCursor:
    def __init__(self, log, rows):
        self.log, self.rows, self.description, self.sfqid = log, rows, None, None

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        if "information_schema.tables" in sql:
            self.description = [("TABLE_TYPE",)]
        elif sql.lower().startswith("select"):
            self.description = [("N",)]

    def fetchall(self):
        return self.rows

    def fetchmany(self, n):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        pass


class FakeSnowConn:
    def __init__(self, rows):
        self.log, self.rows = [], rows

    def cursor(self):
        return FakeSnowCursor(self.log, list(self.rows))


def test_snowflake_query_tag_and_change_signal():
    conn = FakeSnowConn([("BASE TABLE", "2026-01-01 10:00", 10, 100)])
    eng = SnowflakeEngine(Connection("sf", "snowflake", {"account": "a"}), _conn=conn)
    eng.tags = {"tool": "gulliver", "kind": "run", "agent": "a1"}
    eng.execute("select 1", preview_rows=1)
    tag_calls = [c for c in conn.log if "QUERY_TAG" in c[0]]
    assert tag_calls and json.loads(tag_calls[0][1][0])["agent"] == "a1"
    assert eng.data_version(["PUBLIC.EVENTS"]).startswith("snowflake:PUBLIC.EVENTS:2026-01-01 10:00")
    assert eng.data_version(["OTHERDB.PUBLIC.EVENTS"]) is None
    view = SnowflakeEngine(Connection("sf", "snowflake", {"account": "a"}), _conn=FakeSnowConn([("VIEW", "x", 1, 1)]))
    assert view.data_version(["V"]) is None


def test_postgres_application_name():
    name = application_name({"kind": "run", "agent": "claude-code", "receipt": "gv-20261102-120000-run-ab12"})
    assert name == "gulliver:gv-20261102-120000-run-ab12:claude-code"
    assert len(application_name({"kind": "run", "agent": "ü" * 100})) <= 63


@pytest.mark.skipif(not os.environ.get("GULLIVER_TEST_PG_DSN"), reason="set GULLIVER_TEST_PG_DSN to run live Postgres")
def test_postgres_live_application_name(state):
    s = load_settings(dsn=os.environ["GULLIVER_TEST_PG_DSN"])
    with use(Identity("pg-agent")):
        res = core.run(s, "select application_name from pg_stat_activity where pid = pg_backend_pid()")
    assert res["status"] == "ok", res
    assert res["preview"][0]["application_name"] == f"gulliver:{res['receipt_id']}:pg-agent"


# -- spend report ---------------------------------------------------------------------------------------
def test_spend_by_agent_day_and_table(billed, capsys):
    s = billed()
    with use(Identity("a")):
        core.run(s, Q)
        core.run(s, "select count(*) from events e join users u on e.user_id = u.user_id")
    with use(Identity("b")):
        core.run(s, Q)
    by_agent = runaway.spend(s, by="agent")
    assert [(r["agent"], r["usd"]) for r in by_agent["rows"]] == [("a", 0.6), ("b", 0.3)]
    assert by_agent["total"]["usd"] == pytest.approx(0.9)
    by_table = {r["table"]: r for r in runaway.spend(s, by="table")["rows"]}
    assert by_table["users"]["usd"] == pytest.approx(0.75) and by_table["events"]["usd"] == pytest.approx(0.15)
    assert sum(r["usd"] for r in by_table.values()) == pytest.approx(0.9)
    day = runaway.spend(s, by="day")["rows"]
    assert len(day) == 1 and day[0]["executions"] == 3
    assert runaway.spend(s, by="nope")["status"] == "error"
    csv_text = runaway.render_spend(by_agent, "csv")
    assert csv_text.splitlines()[0] == "agent,usd,executions,runs,bytes"
    assert "| a | $0.6000 |" in runaway.render_spend(by_agent)


def test_cli_agent_flag_and_spend(demo_db, state, capsys):
    assert main(["--dsn", demo_db, "--agent", "cli-bot", "run", "--json", Q]) == 0
    capsys.readouterr()
    assert main(["--dsn", demo_db, "spend", "--by", "agent", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["rows"][0]["agent"] == "cli-bot"
    assert main(["--dsn", demo_db, "spend", "--by", "table", "--format", "csv"]) == 0
    assert capsys.readouterr().out.startswith("table,usd")


# -- audit chain ------------------------------------------------------------------------------------------
def _some_history(settings):
    core.preflight(settings, Q)
    core.run(settings, Q)
    core.run(settings, "select count(*) from users")


def test_audit_verify_intact(settings):
    _some_history(settings)
    res = runaway.audit_verify(settings)
    assert res["status"] == "ok", res
    assert res["ledger"]["chained"] == 2 and res["receipts"]["files_checked"] == 3
    assert runaway.audit_verify(settings, head=res["ledger"]["head"])["status"] == "ok"


def test_audit_detects_an_edited_receipt(settings, capsys):
    _some_history(settings)
    rid = core.get_receipt(settings, "latest", fmt="json")["receipt"]["receipt_id"]
    path = settings.state_dir / "receipts" / f"{rid}.json"
    path.write_text(path.read_text().replace('"status": "ok"', '"status": "block"'))
    res = runaway.audit_verify(settings)
    assert res["status"] == "fail"
    assert any(rid in p and "changed" in p for p in res["problems"])
    assert main(["audit", "verify", "--json"]) == 4


def test_audit_detects_edited_and_removed_ledger_lines(settings):
    _some_history(settings)
    core.run(settings, "select 1 as x")
    lines = (settings.state_dir / "ledger.jsonl").read_text().splitlines()
    head = runaway.audit_verify(settings)["ledger"]["head"]
    edited = lines[0].replace('"cost_usd": 0.0', '"cost_usd": 0.0001')
    (settings.state_dir / "ledger.jsonl").write_text("\n".join([edited, *lines[1:]]) + "\n")
    assert any("line 1 was changed" in p for p in runaway.audit_verify(settings)["problems"])
    (settings.state_dir / "ledger.jsonl").write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert any("line 2 does not follow" in p for p in runaway.audit_verify(settings)["problems"])
    (settings.state_dir / "ledger.jsonl").write_text("\n".join(lines[:2]) + "\n")   # newest line removed
    assert runaway.audit_verify(settings)["status"] == "ok"                          # ... invisible on its own
    assert runaway.audit_verify(settings, head=head)["status"] == "fail"            # ... but not with a kept head


def test_audit_key_makes_hashes_hmacs(settings, monkeypatch):
    monkeypatch.setenv(audit.KEY_ENV, "s3cret")
    _some_history(settings)
    assert runaway.audit_verify(settings)["status"] == "ok"
    assert all(e["alg"] == audit.HMAC for e in ledger(settings))
    monkeypatch.delenv(audit.KEY_ENV)
    res = runaway.audit_verify(settings)
    assert res["status"] == "fail" and any("set GULLIVER_AUDIT_KEY" in p for p in res["problems"])


def test_audit_counts_lines_written_before_the_chain(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "ledger.jsonl").write_text(json.dumps({"at": "2026-01-01T00:00:00+00:00", "kind": "run",
                                                                 "cost_usd": 0.0}) + "\n")
    _some_history(settings)
    res = runaway.audit_verify(settings)
    assert res["status"] == "warn" and not res["problems"]
    assert res["ledger"]["entries"] == 3 and res["ledger"]["chained"] == 2


# -- config and defaults ---------------------------------------------------------------------------------
def test_budget_config(tmp_path, monkeypatch, state):
    (tmp_path / "gulliver.toml").write_text('[budget]\nagent_day_usd = { default = 5, "claude-code" = 10 }\n'
                                            "session_usd = 3\n")
    s = load_settings()
    assert s.budget.agent_day_usd == {"default": 5.0, "claude-code": 10.0}
    assert runaway.agent_limit(s, "claude-code") == 10.0 and runaway.agent_limit(s, "other") == 5.0
    monkeypatch.setenv("GULLIVER_AGENT_DAY_USD", "2")
    assert load_settings().budget.agent_day_usd == {"default": 2.0}
    with pytest.raises(ConfigError):
        load_settings(budget_overrides={"agent_day_usd": {"x": "lots"}})
    b = load_settings(config_path=None, budget_overrides={}).budget
    assert b.max_repeats == 5 and b.repeat_window_min == 30 and b.velocity_usd > b.approval_over_usd


def test_status_shows_the_caller(settings):
    caller = core.status(settings)["caller"]
    assert caller["agent"] == "local" and caller["agent_day_limit_usd"] is None
    assert caller["repeat_guard"]["max_repeats"] == 5
