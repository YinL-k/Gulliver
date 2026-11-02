"""gulliver review: a temporary git repo with SQL models and a small DuckDB warehouse."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess

import duckdb
import pytest

from gulliver import review
from gulliver.cli import main
from gulliver.config import ConfigError, load_settings
from gulliver.mcp_server import build_server

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

BASE = {
    "stg_events.sql": "select event_id, user_id, amount, ts from {{ source('raw', 'events') }}\n",
    "user_totals.sql": "select user_id, sum(amount) as total, count(*) as n\nfrom {{ ref('stg_events') }}\n"
                       "group by 1\n",
    "report.sql": "select user_id, total from {{ ref('user_totals') }} where n > 1\n",
    "country.sql": "select country, count(*) as users from {{ source('raw', 'users') }} group by 1\n",
    "extra.sql": "select user_id, country from {{ source('raw', 'users') }}\n",
}
HEAD = {
    # removes total and n, adds a self-join that multiplies rows, and SELECT *
    "user_totals.sql": "select e.*, e2.amount as other_amount\nfrom {{ ref('stg_events') }} e\n"
                       "join {{ ref('stg_events') }} e2 on e.user_id = e2.user_id\n",
    # formatting and a comment only
    "country.sql": "SELECT country,\n       COUNT(*) AS users -- per country\nFROM {{ source('raw', 'users') }}\n"
                   "GROUP BY 1\n",
    # one column added
    "extra.sql": "select user_id, country, upper(country) as country_code from {{ source('raw', 'users') }}\n",
}


def git(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-c", "commit.gpgsign=false", "-C", str(repo), *args], check=True,
                          capture_output=True, env=env).stdout.decode()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    models = root / "models"
    models.mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "checkout", "-q", "-b", "main")
    for name, sql in BASE.items():
        (models / name).write_text(sql, encoding="utf-8")
    (root / "README.md").write_text("not a model\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    git(root, "checkout", "-q", "-b", "feature")
    for name, sql in HEAD.items():
        (models / name).write_text(sql, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "head")
    return root


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    path = tmp_path_factory.mktemp("wh") / "wh.duckdb"
    con = duckdb.connect(str(path))
    con.execute("create schema raw")
    con.execute("create table raw.events as select i as event_id, i % 500 as user_id, (i % 97) * 1.5 as amount, "
                "timestamp '2026-01-01' + to_seconds(i) as ts from range(60000) t(i)")
    con.execute("create table raw.users as select i as user_id, ['NL','DE','US'][i % 3 + 1] as country "
                "from range(500) t(i)")
    # production builds of the models (refs resolve to these, like dbt --defer)
    con.execute("create table stg_events as select event_id, user_id, amount, ts from raw.events")
    con.execute("create table user_totals as select user_id, sum(amount) as total, count(*) as n "
                "from stg_events group by 1")
    con.close()
    return str(path)


def _by_model(result):
    return {m["model"]: m for m in result["models"]}


# -- classification ---------------------------------------------------------------------------------
def test_classify_change_kinds():
    c = review.classify_change
    assert c("select a, b from t", "SELECT a,\n  b -- x\nFROM t")["classification"] == "cosmetic"
    added = c("select a from t", "select a, b + 1 as c from t")
    assert added["classification"] == "non_breaking" and added["columns"]["added"] == ["c"]
    where = c("select a from t", "select a from t where a > 1")
    assert where["classification"] == "breaking" and where["targets"] == ["*"]
    assert "WHERE" in where["reasons"][0]
    retyped = c("select cast(a as int) as a from t", "select cast(a as varchar) as a from t")
    assert retyped["classification"] == "breaking" and retyped["columns"]["retyped"][0]["to"] == "TEXT"
    assert retyped["targets"] == ["a"]
    removed = c("select a, b from t", "select a from t")
    assert removed["columns"]["removed"] == ["b"] and removed["targets"] == ["b"]
    assert c("select a from t", "select a from t order by a")["classification"] == "cosmetic"
    assert c("select a from t", "select a from t order by a limit 5")["classification"] == "breaking"
    assert c("select a, b from t", "select b, a from t")["classification"] == "non_breaking"
    # renaming or adding a table alias is not a change; a self-join keeps its aliases apart
    assert c("select a, b from s.t", "select x.a, x.b from s.t as x")["classification"] == "cosmetic"
    assert c("select x.a from t x join u y on x.id = y.id", "select q.a from t q join u r on q.id = r.id"
             )["classification"] == "cosmetic"
    assert c("select e.a from t e join t f on e.id = f.id", "select f.a from t e join t f on e.id = f.id"
             )["classification"] == "breaking"
    assert c("select a from t", "select from where")["classification"] == "unknown"


def test_unaliased_expressions_are_compared_by_position():
    c = review.classify_change
    r = c("SELECT id, a + 1, b + 1 FROM t", "SELECT id, a + 2, b + 1 FROM t")
    assert r["classification"] == "breaking" and r["columns"]["changed"] == ["_col_1"]
    assert r["targets"] == ["_col_1"]  # the name sqlglot's lineage gives that column
    r = c("SELECT id, SUM(a), SUM(b) FROM t GROUP BY id", "SELECT id, SUM(c), SUM(b) FROM t GROUP BY id")
    assert r["classification"] == "breaking" and r["columns"]["changed"] == ["_col_1"]
    assert c("SELECT id, a + 1, b + 1 FROM t", "SELECT id,\n a + 1, b + 1 FROM t")["classification"] == "cosmetic"


def test_same_named_tables_in_two_schemas_keep_their_aliases():
    c = review.classify_change
    join = "FROM prod.orders AS x JOIN archive.orders AS y ON x.id = y.id"
    assert c(f"SELECT x.amount {join}", f"SELECT y.amount {join}")["classification"] == "breaking"
    assert c("SELECT x.amount FROM prod.orders AS x LEFT JOIN archive.orders AS y ON x.id = y.id",
             "SELECT x.amount FROM prod.orders AS x LEFT JOIN archive.orders AS y ON y.id = y.id"
             )["classification"] == "breaking"
    # with two tables named orders, renaming the aliases is not treated as cosmetic either
    assert c(f"SELECT x.amount {join}",
             "SELECT p.amount FROM prod.orders AS p JOIN archive.orders AS q ON p.id = q.id"
             )["classification"] == "breaking"  # not canonicalized: conservative, never 'cosmetic' by mistake


# -- git mode -------------------------------------------------------------------------------------------
def test_review_offline_classifies_and_finds_downstream(repo, state):
    r = review.review(None, base="main", head="feature", models_dir=str(repo / "models"))
    assert r["status"] == "block"
    models = _by_model(r)
    assert set(models) == {"user_totals", "country", "extra"}
    ut = models["user_totals"]
    assert ut["classification"] == "breaking"
    assert set(ut["columns"]["removed"]) == {"total", "n"}
    assert [d["model"] for d in ut["downstream"]] == ["report"]
    assert "GV001" in {f["rule"] for f in ut["lint"]["introduced"]}
    assert models["country"]["classification"] == "cosmetic"
    assert models["extra"]["classification"] == "non_breaking"
    assert models["extra"]["columns"]["added"] == ["country_code"]
    kinds = {v["kind"] for v in r["violations"]}
    assert kinds == {"breaking_with_downstream"}
    assert "cost" not in r and any("not estimated" in n for n in r["notes"])
    md = review.render_markdown(r)
    assert md.startswith(review.MARKER) and "BLOCK" in md and "`report`" in md


def test_review_with_connection_estimates_cost_delta(repo, warehouse, state):
    settings = load_settings(dsn=warehouse)
    r = review.review(settings, base="main", head="feature", models_dir=str(repo / "models"))
    ut = _by_model(r)["user_totals"]
    assert ut["cost"]["bytes_delta"] > 0
    assert ut["cost"]["accuracy"] == "heuristic"
    assert ut["cost"]["usd_delta_per_month"] == 0
    assert r["cost"]["bytes_delta"] > 0 and r["cost"]["engine"] == "duckdb"
    assert "cost" not in _by_model(r)["country"]  # cosmetic: not estimated
    assert "Estimates only: nothing was run." in r["notes"]


def test_review_cost_policy_blocks_big_increase(repo, warehouse, state):
    settings = load_settings(dsn=warehouse)
    policy = review.Policy(max_increase_pct=10, ignore_below_bytes=0, fail_on=("cost",))
    r = review.review(settings, base="main", head="feature", models_dir=str(repo / "models"), policy=policy)
    assert r["status"] == "block"
    assert {v["kind"] for v in r["violations"]} == {"cost"}
    loose = review.Policy(fail_on=())
    r = review.review(settings, base="main", head="feature", models_dir=str(repo / "models"), policy=loose)
    assert r["status"] == "warn" and not r["violations"] and r["flags"]


def test_review_rehearse_flags_new_superlinear_growth(repo, warehouse, state):
    settings = load_settings(dsn=warehouse)
    r = review.review(settings, base="main", head="feature", models_dir=str(repo / "models"), rehearse=True)
    growth = _by_model(r)["user_totals"]["growth"]
    assert growth["head_exponent"] > 1.3 >= growth["base_exponent"]
    assert growth["superlinear"] is True
    assert "superlinear" in {v["kind"] for v in r["violations"]}
    assert growth["fractions"] == [0.01, 0.03]


def test_review_missing_base_ref_blocks(repo, state):
    r = review.review(None, base="origin/does-not-exist", head="feature", models_dir=str(repo / "models"))
    assert r["status"] == "block" and r["reason"] == "base_missing"
    assert "fetch-depth" in r["next_step"]


def test_review_unknown_models_folder_blocks(repo, state):
    r = review.review(None, base="main", head="feature", models_dir="modles", repo=str(repo))
    assert r["status"] == "block" and r["reason"] == "models_dir_missing"
    assert "modles" in r["next_step"]
    # a folder that exists in the trees is reviewed as usual
    assert review.review(None, base="main", head="feature", models_dir=str(repo / "models"))["status"] == "block"


def test_review_file_name_with_a_newline_does_not_shift_contents(repo, state):
    (repo / "models" / "a\nb.sql").write_text("select 1 as x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "odd name")
    texts = review._read_tree(repo, "HEAD", "models")
    assert texts["models/a\nb.sql"] == "select 1 as x\n"
    assert texts["models/report.sql"] == BASE["report.sql"]
    assert texts["models/user_totals.sql"] == HEAD["user_totals.sql"]
    r = review.review(None, base="main", head="HEAD", models_dir=str(repo / "models"))
    assert r["status"] == "block" and "breaking_with_downstream" in {v["kind"] for v in r["violations"]}
    assert [d["model"] for d in _by_model(r)["user_totals"]["downstream"]] == ["report"]


def test_review_model_that_cannot_be_tokenized_is_not_compiled(repo, state):
    (repo / "models" / "extra.sql").write_text("SELECT 'oops AS a\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "broken quote")
    r = review.review(None, base="main", head="HEAD", models_dir=str(repo / "models"))
    assert r["status"] in ("warn", "block")
    assert _by_model(r)["extra"]["classification"] == "unknown"


def test_review_no_changes_is_ok(repo, state):
    r = review.review(None, base="feature", head="feature", models_dir=str(repo / "models"))
    assert r["status"] == "ok" and r["models"] == []


def test_review_deleted_and_renamed_models(repo, state):
    git(repo, "rm", "-q", "models/report.sql")
    git(repo, "mv", "models/stg_events.sql", "models/stg_events_v2.sql")
    git(repo, "commit", "-q", "-m", "delete and rename")
    r = review.review(None, base="main", head="HEAD", models_dir=str(repo / "models"))
    models = _by_model(r)
    assert models["report"]["change"] == "removed" and models["report"]["classification"] == "breaking"
    renamed = models["stg_events_v2"]
    assert renamed["change"] == "renamed" and renamed["renamed_from"] == "stg_events"
    assert renamed["classification"] == "breaking"
    assert "user_totals" in {d["model"] for d in renamed["downstream"]}


def test_review_notes_uncommitted_changes(repo, state):
    (repo / "models" / "extra.sql").write_text("select 1 as x\n")
    r = review.review(None, base="main", head="HEAD", models_dir=str(repo / "models"))
    assert any("uncommitted" in n for n in r["notes"])


def test_review_reports_models_the_engine_cannot_estimate(repo, warehouse, state):
    before = git(repo, "rev-parse", "HEAD").strip()
    (repo / "models" / "extra.sql").write_text("select user_id, not_built_yet from {{ source('raw', 'users') }}\n")
    git(repo, "commit", "-q", "-am", "uses a column production does not have")
    r = review.review(load_settings(dsn=warehouse), base=before, head="HEAD", models_dir=str(repo / "models"))
    extra = _by_model(r)["extra"]
    assert "not estimated" in extra["cost"]["note"] and r["cost"]["not_estimated"] == 1
    assert r["status"] == "warn"


def test_money_per_month_and_usd_limit(repo, warehouse, state, monkeypatch):
    """A billed engine (BigQuery-like exact dry runs), faked at the preflight step."""
    def fake_preflight(self, sql):
        tib = 1024**4
        big = "e2" in sql  # the head version of user_totals scans twice
        return {"status": "ok", "lint": [], "estimate": {"bytes": tib * (2 if big else 1), "accuracy": "exact",
                                                         "cost_usd": 6.25 * (2 if big else 1)}}

    monkeypatch.setattr(review._Estimator, "_preflight", fake_preflight)
    settings = load_settings(dsn=warehouse)
    policy = review.Policy(runs_per_day=4, max_increase_usd=500, fail_on=("cost",))
    r = review.review(settings, base="main", head="feature", models_dir=str(repo / "models"), policy=policy)
    ut = _by_model(r)["user_totals"]
    assert ut["cost"]["usd_delta_per_run"] == 6.25 and ut["cost"]["usd_delta_per_month"] == 750.0
    assert ut["cost"]["bytes_delta_pct"] == 100.0 and r["cost"]["accuracy"] == "exact"
    assert {v["model"] for v in r["violations"]} == {"(all)"}  # +$750/month > $500; +100% is not over 100%
    assert "+$750.00 per month" in review.render_markdown(r)


def test_jinja_helpers():
    assert review._engine_sql("select a from {{ ref('stg') }}", "m", "analytics") == "select a from analytics.stg"
    assert review._engine_sql("select a from {{ ref('stg') }}", "m", "") == "select a from stg"
    assert not review._approximate("{{ config(materialized='table') }} select * from {{ ref('a') }}")
    assert review._approximate("select {{ dbt_utils.star(ref('a')) }} from {{ ref('a') }}")
    assert review._approximate("select a from t {% if is_incremental() %} where ts > 1 {% endif %}")


# -- manifest mode ---------------------------------------------------------------------------------------
def _manifest(path, models, project="p1"):
    nodes = {f"model.proj.{name}": {"resource_type": "model", "name": name, "compiled_code": sql,
                                    "original_file_path": f"models/{name}.sql",
                                    "depends_on": {"nodes": [f"model.proj.{d}" for d in deps]}}
             for name, (sql, deps) in models.items()}
    path.write_text(json.dumps({"metadata": {"project_id": project, "generated_at": "2026-11-01"}, "nodes": nodes}))
    return str(path)


def test_review_manifests(tmp_path, state):
    base = _manifest(tmp_path / "base.json", {
        "orders": ("select id, amount, status from raw.orders", []),
        "revenue": ("select sum(amount) as revenue from orders", ["orders"]),
    })
    head = _manifest(tmp_path / "head.json", {
        "orders": ("select id, status from raw.orders", []),
        "revenue": ("select sum(amount) as revenue from orders", ["orders"]),
        "new_model": ("select id from orders", ["orders"]),
    })
    r = review.review(None, manifest_base=base, manifest_head=head)
    models = _by_model(r)
    assert models["orders"]["classification"] == "breaking"
    assert [d["model"] for d in models["orders"]["downstream"]] == ["revenue"]
    assert models["new_model"]["change"] == "added"
    assert r["status"] == "block"


def test_review_missing_or_foreign_base_manifest_blocks(tmp_path, state):
    head = _manifest(tmp_path / "head.json", {"a": ("select 1 as x", [])})
    r = review.review(None, manifest_base=str(tmp_path / "nope.json"), manifest_head=head)
    assert r["status"] == "block" and r["reason"] == "base_manifest_missing"
    other = _manifest(tmp_path / "other.json", {"a": ("select 1 as x", [])}, project="p2")
    assert review.review(None, manifest_base=other, manifest_head=head)["reason"] == "base_manifest_stale"


# -- policy ----------------------------------------------------------------------------------------------------
def test_policy_from_toml_and_errors(tmp_path, state):
    cfg = tmp_path / "gulliver.toml"
    cfg.write_text('[review]\nmax_increase_pct = 20\nrunds_per_day = 2\n')
    with pytest.raises(ConfigError):
        review.load_policy(str(cfg))
    cfg.write_text('[review]\nmax_increase_pct = 20\nruns_per_day = 24\nfail_on = ["breaking", "superlinear"]\n')
    policy = review.load_policy(str(cfg))
    assert policy.max_increase_pct == 20 and policy.runs_per_day == 24
    assert policy.fails("breaking_with_downstream") and not policy.fails("cost")
    assert review.load_policy(str(cfg), {"fail_on": "none"}).fail_on == ()
    with pytest.raises(ConfigError):
        review.Policy.from_mapping({"fail_on": ["everything"]})


# -- CLI and MCP ---------------------------------------------------------------------------------------------------
def test_cli_review_writes_report_and_exit_code(repo, warehouse, state, tmp_path, capsys):
    out = tmp_path / "review.md"
    code = main(["--dsn", warehouse, "review", "--models", str(repo / "models"), "--base", "main", "--head", "feature",
                 "-o", str(out), "--json"])
    assert code == 2
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "block" and data["summary"]["breaking"] == 1
    assert out.read_text(encoding="utf-8").startswith(review.MARKER)
    code = main(["review", "--models", str(repo / "models"), "--base", "main", "--head", "feature",
                 "--fail-on", "none"])
    assert code == 0
    assert review.MARKER in capsys.readouterr().out


def test_cli_review_json_file_and_bad_policy(repo, state, tmp_path, capsys):
    out = tmp_path / "review.json"
    main(["review", "--models", str(repo / "models"), "--base", "main", "--head", "feature", "--format", "json",
          "-o", str(out)])
    assert json.loads(out.read_text())["action"] == "review"
    assert main(["review", "--models", str(repo / "models"), "--base", "main", "--fail-on", "nope"]) == 1


def test_mcp_review_tool(repo, warehouse, state, monkeypatch):
    monkeypatch.setenv("GULLIVER_DSN", warehouse)
    server = build_server()
    result = asyncio.run(server.call_tool("gulliver_review", {"base": "main", "head": "feature",
                                                              "models_dir": str(repo / "models"), "markdown": True}))
    text = json.dumps(result if isinstance(result, (dict, list)) else
                      (result.model_dump() if hasattr(result, "model_dump") else str(result)), default=str)
    assert "breaking_with_downstream" in text and "gulliver-review" in text


def test_action_yml_is_wired():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "action.yml").read_text(encoding="utf-8")
    assert "gulliver review" in text and review.MARKER in text and "GITHUB_STEP_SUMMARY" in text
    assert "upload-sarif" in text and "inputs.sarif" in text
    # the sticky comment is only ever our own: right author and the marker at the start, not anywhere
    assert '.user.login == \\"${login}\\"' in text and 'startswith(\\"${marker}\\")' in text
    assert "contains(" not in text
    # inputs reach the shell only through env: never ${{ }} inside a run script (script injection)
    in_run, indent = False, 0
    for line in text.splitlines():
        stripped = line.lstrip()
        if in_run and stripped and len(line) - len(stripped) <= indent:
            in_run = False
        if stripped.startswith("run: |"):
            in_run, indent = True, len(line) - len(stripped)
            continue
        if in_run:
            assert "${{" not in line, line
