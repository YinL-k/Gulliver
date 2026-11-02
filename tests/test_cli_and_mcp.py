import asyncio
import json

from gulliver.cli import main
from gulliver.mcp_server import build_server, export_tools

TOOLS = {"gulliver_status", "gulliver_inspect", "gulliver_lint", "gulliver_preflight", "gulliver_rehearse",
         "gulliver_run", "gulliver_receipt", "gulliver_impact", "gulliver_drift", "gulliver_translate",
         "gulliver_diff", "gulliver_incremental", "gulliver_check", "gulliver_fix"}


def test_cli_status_json_after_command(demo_db, state, capsys):
    assert main(["--dsn", demo_db, "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "ok"


def test_cli_exit_codes(demo_db, state, capsys):
    assert main(["lint", "select e.id from events e, users u"]) == 2
    assert main(["--dsn", demo_db, "preflight", "--json", "select count(*) from events"]) == 0
    assert main(["--dsn", demo_db, "run", "delete from events"]) == 2


def test_cli_sql_from_file(demo_db, state, tmp_path, capsys):
    q = tmp_path / "q.sql"
    q.write_text("select country, count(*) from users group by 1")
    assert main(["--dsn", demo_db, "--json", "run", "-f", str(q)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["actual"]["rows"] == 8


def test_tool_export_formats():
    mcp_tools = export_tools("mcp")
    assert {t["name"] for t in mcp_tools} == TOOLS
    openai = export_tools("openai")
    assert all(t["type"] == "function" and t["function"]["parameters"]["type"] == "object" for t in openai)
    gemini = export_tools("gemini")
    text = json.dumps(gemini)
    assert "anyOf" not in text and "title" not in json.dumps(gemini["function_declarations"][0]["parameters"])


def test_mcp_server_lists_and_calls_tools(demo_db, state, monkeypatch):
    monkeypatch.setenv("GULLIVER_DSN", demo_db)
    server = build_server()
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == TOOLS
    result = asyncio.run(server.call_tool("gulliver_lint", {"sql": "select * from events"}))
    text = json.dumps(result if isinstance(result, (dict, list)) else
                      (result.model_dump() if hasattr(result, "model_dump") else str(result)), default=str)
    assert "GV001" in text


def test_cli_receipt_output_file(demo_db, state, tmp_path, capsys):
    assert main(["--dsn", demo_db, "preflight", "select count(*) from events"]) == 0
    md = tmp_path / "receipt.md"
    assert main(["--dsn", demo_db, "receipt", "latest", "--format", "markdown", "-o", str(md)]) == 0
    assert "select count(*) from events" in md.read_text(encoding="utf-8").lower()
    js = tmp_path / "receipt.json"
    assert main(["--dsn", demo_db, "receipt", "latest", "--format", "json", "-o", str(js)]) == 0
    assert json.loads(js.read_text(encoding="utf-8"))["receipt_id"].startswith("gv-")


def test_cli_translate_output_file(tmp_path, capsys):
    out = tmp_path / "model.bq.sql"
    assert main(["translate", "--from", "duckdb", "--to", "bigquery", "-o", str(out), "--json",
                 "select my_udf(x), date_trunc('day', ts) from t"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["warnings"]  # still printed
    assert out.read_text(encoding="utf-8") == data["sql"]
