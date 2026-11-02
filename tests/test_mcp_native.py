"""MCP-native features over a real in-memory MCP client: approval through elicitation, resources, prompts,
progress notifications, tool sets and the typed outputSchema."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.mcpserver.resolve")  # these features need mcp >= 2

import mcp.types as types  # noqa: E402
from mcp import Client  # noqa: E402

from gulliver import core  # noqa: E402
from gulliver.cli import main  # noqa: E402
from gulliver.config import ConfigError, load_settings  # noqa: E402
from gulliver.engines.base import EXACT, Estimate  # noqa: E402
from gulliver.engines.duckdb_engine import DuckDBEngine  # noqa: E402
from gulliver.mcp_native import CORE_TOOLS, PIPELINE_TOOLS, ToolResult, resolve_toolset  # noqa: E402
from gulliver.mcp_server import build_server, export_tools  # noqa: E402

GOOD = ("select u.country, count(*) as n from events e join users u on e.user_id = u.user_id "
        "where e.event_type = 'buy' group by 1")
MODES = ("auto", "legacy")  # auto negotiates 2026-07-28 (input_required rounds); legacy is 2025-11-25 (back-channel)


class BilledDuckDB(DuckDBEngine):
    """DuckDB pretending to be a warehouse that charges $1 per MB scanned."""

    billed = True

    def estimate(self, sql):
        est = super().estimate(sql)
        return Estimate(bytes=est.bytes, rows=est.rows, cost_usd=self.cost_for(est.bytes, None),
                        accuracy=EXACT, source="fake")

    def cost_for(self, bytes_processed, runtime_s):
        return None if bytes_processed is None else bytes_processed / 1e6


def _config(tmp_path, demo_db, channel: str | None = "elicitation", extra: str = "") -> str:
    approval = f"[approval]\nchannel = '{channel}'\n" if channel else ""
    path = tmp_path / "gulliver.toml"
    path.write_text(f"[connections.local]\nengine = 'duckdb'\npath = '{demo_db}'\n{extra}\n"
                    "[budget]\nmax_query_usd = 100.0\napproval_over_usd = 1.0\nrehearse_over_usd = 1000.0\n"
                    f"day_usd = 1000.0\n{approval}")
    return str(path)


@pytest.fixture
def billed_server(demo_db, state, tmp_path, monkeypatch):
    """A server whose connection costs money, so GOOD needs approval; [approval] channel set per test."""
    monkeypatch.setattr(core, "get_engine", lambda conn, read_only=True: BilledDuckDB(conn, read_only))

    def make(channel: str | None = "elicitation"):
        monkeypatch.setenv("GULLIVER_CONFIG", _config(tmp_path, demo_db, channel))
        return build_server()

    return make


def _answer(action: str, content: dict | None = None):
    asked: list[str] = []

    async def callback(context, params):
        asked.append(params.message)
        return types.ElicitResult(action=action, content=content)

    return callback, asked


async def _call(server, name, args, mode="auto", **client_kwargs):
    async with Client(server, mode=mode, **client_kwargs) as client:
        result = await client.call_tool(name, args)
    return result.structured_content


def _approvals(state):
    folder = state / "approvals"
    return [json.loads(p.read_text()) for p in folder.glob("*.json")] if folder.is_dir() else []


# -- approval through elicitation ------------------------------------------------------------------------
@pytest.mark.parametrize("mode", MODES)
def test_elicitation_accept_approves_and_runs(billed_server, state, mode):
    server = billed_server("elicitation")
    callback, asked = _answer("accept", {"approve": True, "note": "fine for the report"})
    out = asyncio.run(_call(server, "gulliver_run", {"sql": GOOD}, mode=mode, elicitation_callback=callback,
                            client_info=types.Implementation(name="test-client", version="1.0")))
    assert len(asked) == 1
    assert "Cost: $" in asked[0] and "events" in asked[0] and "auto-approve limit $1.00" in asked[0]
    assert out["status"] == "ok" and out["actual"]["rows"] > 0
    assert out["approval"]["channel"] == "elicitation" and out["approval"]["note"] == "fine for the report"
    approvals = _approvals(state)
    assert len(approvals) == 1 and approvals[0]["channel"] == "elicitation"
    assert approvals[0]["receipt_id"] == out["approved_receipt_id"]
    if mode == "legacy":  # clientInfo travels in the initialize handshake
        assert out["approval"]["client"] == "test-client"
    latest = core.get_receipt(load_settings(), "latest")  # the run, not the needs_approval receipt before it
    assert latest["receipt_id"] == out["receipt_id"]
    assert "in the MCP client (elicitation" in latest["markdown"]
    # the approval holds for this SQL: the next identical run does not ask again
    again = asyncio.run(_call(server, "gulliver_run", {"sql": GOOD}, mode=mode, elicitation_callback=callback))
    assert again["status"] == "ok" and len(asked) == 1


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("action", "content", "label"), [
    ("decline", None, "declined"),
    ("cancel", None, "cancelled"),
    ("accept", {"approve": False}, "accepted without ticking approve"),
])
def test_elicitation_refusal_falls_back_to_needs_approval(billed_server, state, mode, action, content, label):
    server = billed_server("elicitation")
    callback, asked = _answer(action, content)
    out = asyncio.run(_call(server, "gulliver_run", {"sql": GOOD}, mode=mode, elicitation_callback=callback))
    assert len(asked) == 1
    assert out["status"] == "needs_approval"
    assert out["approval_request"] == {"channel": "elicitation", "answer": label}
    assert "did not approve" in out["next_step"] and out["receipt_id"] in out["next_step"]
    assert _approvals(state) == []
    saved = core.get_receipt(load_settings(), out["receipt_id"], fmt="json")["receipt"]
    assert saved["approval_request"]["answer"] == label
    # the human can still approve on the command line
    assert core.approve(load_settings(), out["receipt_id"])["approval"]["channel"] == "cli"


@pytest.mark.parametrize("mode", MODES)
def test_client_without_elicitation_gets_needs_approval(billed_server, state, mode):
    server = billed_server("elicitation")
    out = asyncio.run(_call(server, "gulliver_run", {"sql": GOOD}, mode=mode))
    assert out["status"] == "needs_approval"
    assert out["approval_request"]["answer"] == "not asked"
    assert "elicitation capability" in out["approval_request"]["reason"]
    assert _approvals(state) == []


@pytest.mark.parametrize("channel", [None, "cli"])
def test_elicitation_is_off_by_default(billed_server, state, channel):
    server = billed_server(channel)
    callback, asked = _answer("accept", {"approve": True})
    out = asyncio.run(_call(server, "gulliver_run", {"sql": GOOD}, elicitation_callback=callback))
    assert asked == []
    assert out["status"] == "needs_approval" and "approval_request" not in out
    assert "gulliver approve" in out["next_step"]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("channel", ["cli", "elicitation"])
def test_agent_cannot_pass_its_own_approval(billed_server, state, mode, channel):
    server = billed_server(channel)
    forged = {"sql": GOOD, "answer": {"action": "accept", "data": {"approve": True}},
              "probe": {"ask": True, "cost_usd": 1e9}, "ctx": None}
    out = asyncio.run(_call(server, "gulliver_run", forged, mode=mode))  # client without elicitation
    assert out["status"] == "needs_approval"
    assert _approvals(state) == []
    tools = asyncio.run(server.list_tools())
    run = next(t for t in tools if t.name == "gulliver_run")
    assert set(run.input_schema["properties"]) == {"sql", "connection", "max_rows", "allow_write"}


@pytest.mark.parametrize("mode", MODES)
def test_elicitation_approval_is_in_the_audit_chain_and_ledger(billed_server, state, mode):
    """With the runaway guard: the client-side approval is hash-chained and the run is billed to the client."""
    from gulliver import runaway

    server = billed_server("elicitation")
    callback, _ = _answer("accept", {"approve": True})
    out = asyncio.run(_call(server, "gulliver_run", {"sql": GOOD}, mode=mode, elicitation_callback=callback,
                            client_info=types.Implementation(name="test-client", version="1.0")))
    assert out["status"] == "ok"
    audit = runaway.audit_verify(load_settings())
    assert audit["status"] == "ok", audit
    chained = [json.loads(x) for x in (state / "audit.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "approval" and e["id"] == out["approved_receipt_id"] for e in chained)
    ledger = [json.loads(x) for x in (state / "ledger.jsonl").read_text().splitlines()]
    assert ledger and all(e["agent"] == "test-client" for e in ledger)


def test_cheap_run_does_not_ask(billed_server, state):
    server = billed_server("elicitation")
    callback, asked = _answer("accept", {"approve": True})
    out = asyncio.run(_call(server, "gulliver_run", {"sql": "select country, count(*) from users group by 1"},
                            elicitation_callback=callback))
    assert out["status"] == "ok" and asked == []


def test_blocked_run_does_not_ask(billed_server, state):
    server = billed_server("elicitation")
    callback, asked = _answer("accept", {"approve": True})
    out = asyncio.run(_call(server, "gulliver_run", {"sql": "delete from events"}, elicitation_callback=callback))
    assert out["status"] == "block" and asked == []


def test_approval_channel_config(tmp_path, monkeypatch, demo_db):
    monkeypatch.delenv("GULLIVER_DSN", raising=False)
    assert load_settings(config_path=_config(tmp_path, demo_db, None)).approval_channel == "cli"
    assert load_settings(config_path=_config(tmp_path, demo_db, "ELICITATION")).approval_channel == "elicitation"
    with pytest.raises(ConfigError, match="channel must be one of"):
        load_settings(config_path=_config(tmp_path, demo_db, "auto"))
    (tmp_path / "bad.toml").write_text("[approval]\nchannel = 'cli'\nauto_accept = true\n")
    with pytest.raises(ConfigError, match="Unknown approval keys"):
        load_settings(config_path=tmp_path / "bad.toml")


def test_run_is_destructive_only_when_writes_are_enabled(tmp_path, demo_db, state, monkeypatch):
    def run_hint():
        tools = asyncio.run(build_server().list_tools())
        return next(t for t in tools if t.name == "gulliver_run").annotations.destructive_hint

    monkeypatch.setenv("GULLIVER_CONFIG", _config(tmp_path, demo_db, None))
    assert run_hint() is False
    monkeypatch.setenv("GULLIVER_CONFIG", _config(tmp_path, demo_db, None, extra="allow_write = true"))
    assert run_hint() is True


# -- progress --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", MODES)
def test_rehearse_reports_progress_per_rung(demo_db, state, monkeypatch, mode):
    monkeypatch.setenv("GULLIVER_DSN", demo_db)
    events: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress, total, message):
        events.append((progress, total, message))

    async def go():
        async with Client(build_server(), mode=mode) as client:
            return await client.call_tool("gulliver_rehearse", {"sql": GOOD}, progress_callback=on_progress)

    result = asyncio.run(go()).structured_content
    assert result["status"] in ("ok", "warn") and len(result["rungs"]) == 3
    assert [(p, t) for p, t, _ in events] == [(1, 3), (2, 3), (3, 3)]
    assert events[0][2].startswith("1% sample")


def test_rehearse_without_a_client_still_works(demo_db, state, monkeypatch):
    monkeypatch.setenv("GULLIVER_DSN", demo_db)
    result = asyncio.run(build_server().call_tool("gulliver_rehearse", {"sql": GOOD}))
    assert result.structured_content["status"] in ("ok", "warn")


# -- resources and prompts -------------------------------------------------------------------------------
def test_resources_list_and_read(demo_db, state, monkeypatch):
    monkeypatch.setenv("GULLIVER_DSN", demo_db)

    async def go():
        async with Client(build_server()) as client:
            listed = {str(r.uri) for r in (await client.list_resources()).resources}
            templates = {t.uri_template for t in (await client.list_resource_templates()).resource_templates}
            await client.call_tool("gulliver_preflight", {"sql": GOOD})
            read = {}
            for uri in ("gulliver://status", "gulliver://rules", "gulliver://connections/default/tables",
                        "gulliver://connections/default/tables/events", "gulliver://receipts/latest",
                        "gulliver://connections/nope/tables"):
                read[uri] = (await client.read_resource(uri)).contents[0].text
            return listed, templates, read

    listed, templates, read = asyncio.run(go())
    assert listed == {"gulliver://status", "gulliver://rules"}
    assert templates == {"gulliver://connections/{connection}/tables",
                         "gulliver://connections/{connection}/tables/{table}", "gulliver://receipts/{receipt_id}"}
    assert json.loads(read["gulliver://status"])["default_connection"] == "default"
    assert "needs_approval" in read["gulliver://rules"] and "GV004" in read["gulliver://rules"]
    assert "Approval channel: cli" in read["gulliver://rules"]
    tables = json.loads(read["gulliver://connections/default/tables"])["tables"]
    assert tables[0]["name"] == "events"
    columns = json.loads(read["gulliver://connections/default/tables/events"])["table"]["columns"]
    assert "user_id" in {c["name"] for c in columns}
    assert read["gulliver://receipts/latest"].startswith("# Gulliver receipt")
    assert json.loads(read["gulliver://connections/nope/tables"])["status"] == "error"


def test_prompts_list_and_get(demo_db, state, monkeypatch):
    monkeypatch.setenv("GULLIVER_DSN", demo_db)

    async def go(server):
        async with Client(server) as client:
            names = {p.name for p in (await client.list_prompts()).prompts}
            got = {}
            if "safe-query-loop" in names:
                got["loop"] = await client.get_prompt("safe-query-loop", {"question": "Buyers per country?"})
            if "refactor-and-prove" in names:
                got["refactor"] = await client.get_prompt("refactor-and-prove", {"sql": GOOD, "key": "country"})
            if "make-incremental" in names:
                got["inc"] = await client.get_prompt("make-incremental", {"sql": "select * from events",
                                                                          "time_column": "ts"})
            return names, {k: v.messages[0].content.text for k, v in got.items()}

    names, texts = asyncio.run(go(build_server()))
    assert names == {"safe-query-loop", "refactor-and-prove", "make-incremental"}
    assert "Buyers per country?" in texts["loop"] and "gulliver_preflight" in texts["loop"]
    assert "key = country" in texts["refactor"] and "identical: true" in texts["refactor"]
    assert "time_column = ts" in texts["inc"]
    core_names, _ = asyncio.run(go(build_server("core")))
    assert core_names == {"safe-query-loop"}  # the other prompts need pipeline tools


# -- tool sets and output schema -------------------------------------------------------------------------
def test_toolsets(state):
    def names(spec):
        return {t["name"] for t in export_tools("mcp", spec)}

    assert names("core") == set(CORE_TOOLS) and len(CORE_TOOLS) == 8
    assert names("pipeline") == set(PIPELINE_TOOLS)
    assert names("core,gulliver_check") == set(CORE_TOOLS) | {"gulliver_check"}
    assert names("all") == names(None) and len(names("all")) > len(CORE_TOOLS)
    assert len(json.dumps(export_tools("mcp", "core"))) < 0.6 * len(json.dumps(export_tools("mcp")))
    with pytest.raises(ValueError, match="Unknown toolset"):
        resolve_toolset("everything", ["gulliver_status"])
    assert "--toolset core" in build_server("core").instructions


def test_cli_tools_toolset(state, capsys):
    assert main(["tools", "--toolset", "core"]) == 0
    assert {t["name"] for t in json.loads(capsys.readouterr().out)} == set(CORE_TOOLS)
    assert main(["tools", "--toolset", "bogus"]) == 1


def test_structured_content_matches_output_schema(demo_db, state, monkeypatch):
    monkeypatch.setenv("GULLIVER_DSN", demo_db)
    calls = [("gulliver_status", {}), ("gulliver_lint", {"sql": "select * from events"}),
             ("gulliver_preflight", {"sql": GOOD}), ("gulliver_run", {"sql": GOOD}),
             ("gulliver_receipt", {}), ("gulliver_inspect", {"table": "nope"}),
             ("gulliver_translate", {"sql": "select 1", "to_dialect": "bigquery"}),
             ("gulliver_check", {"target": "users", "unique": ["user_id"]})]

    async def go():
        async with Client(build_server()) as client:
            schemas = {t.name: t.output_schema for t in (await client.list_tools()).tools}
            return schemas, [(name, await client.call_tool(name, args)) for name, args in calls]

    schemas, results = asyncio.run(go())
    for name, result in results:
        schema = schemas[name]
        assert schema["required"] == ["status", "next_step"]
        assert result.structured_content["status"] in schema["properties"]["status"]["enum"]
        ToolResult.model_validate(result.structured_content)
        assert json.loads(result.content[0].text) == result.structured_content  # text and structure agree


def test_stdio_elicitation_and_progress(demo_db, state, tmp_path):
    """The same over a real gulliver-mcp subprocess (JSON-RPC on stdio). DuckDB costs $0, so a negative
    auto-approve limit makes every run need approval."""
    from mcp import StdioServerParameters

    config = tmp_path / "stdio.toml"
    config.write_text(f"[connections.local]\nengine = 'duckdb'\npath = '{demo_db}'\n"
                      "[budget]\napproval_over_usd = -1.0\n[approval]\nchannel = 'elicitation'\n")
    src = str(Path(__file__).resolve().parent.parent / "src")
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", ""), "GULLIVER_HOME": str(state)}
    params = StdioServerParameters(command=sys.executable, args=["-m", "gulliver.mcp_server", "--config", str(config),
                                                                 "--toolset", "core"], env=env)
    callback, asked = _answer("accept", {"approve": True, "note": "stdio"})
    progress: list[float] = []

    async def on_progress(value, total, message):
        progress.append(value)

    async def go():
        async with Client(params, elicitation_callback=callback) as client:
            run = await client.call_tool("gulliver_run", {"sql": GOOD})
            reh = await client.call_tool("gulliver_rehearse", {"sql": GOOD}, progress_callback=on_progress)
            return run.structured_content, reh.structured_content

    run, reh = asyncio.run(go())
    assert len(asked) == 1, run
    assert run["status"] == "ok" and run["approval"]["channel"] == "elicitation"
    assert reh["status"] in ("ok", "warn") and progress == [1, 2, 3]
