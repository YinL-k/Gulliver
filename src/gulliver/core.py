"""The Gulliver workflow: status -> inspect -> preflight -> rehearse -> run -> receipt.

Every public function returns a plain JSON-ready dict with at least
  status     ok | warn | block | needs_rehearsal | needs_approval | skipped | error
  next_step  one sentence telling the agent what to do next
The CLI and the MCP server are thin wrappers around these functions.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import __version__
from .config import ConfigError, Connection, Settings
from .engines import Engine, EngineError, TableInfo, dialect_for, get_engine
from .extrapolate import Fit, fit_power_law, overall_confidence
from .lint import Finding, _has_aggregation, _outer_select, lint, summarize
from .sampling import sample_query
from .security import policy_for
from .sqlsafety import Statement, classify
from .store import Store

DEFAULT_FRACTIONS = (0.01, 0.03, 0.1)
SMALL_DATA_ROWS = 10_000
SYSTEM_SAMPLING_ROWS = 1_000_000
REHEARSAL_RUNG_TIMEOUT_S = 300.0

WORKFLOW = ("1 gulliver_inspect (what is big?)  2 gulliver_preflight (cost + lint)  "
            "3 gulliver_rehearse when asked (1% -> 10%, predicts 100%)  4 gulliver_run (gated)  "
            "5 gulliver_receipt (proof for the human)")


# -- helpers ---------------------------------------------------------------------------
@contextmanager
def _engine(settings: Settings, connection: str | None, read_only: bool = True) -> Iterator[tuple[Connection, Engine]]:
    conn = settings.connection(connection)
    engine = get_engine(conn, read_only=read_only)
    try:
        yield conn, engine
    finally:
        engine.close()


def _error(message: str, next_step: str = "Fix the problem above and try again.", **extra: Any) -> dict[str, Any]:
    return {"status": "error", "error": message, "next_step": next_step, **extra}


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value == 0:
        return "$0"
    return f"${value:,.4f}" if value < 1 else f"${value:,.2f}"


def _fmt_bytes(value: int | float | None) -> str:
    if value is None:
        return "unknown"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size} B"  # pragma: no cover


def _table_infos(engine: Engine, statement: Statement) -> tuple[dict[str, TableInfo], list[str]]:
    infos: dict[str, TableInfo] = {}
    notes: list[str] = []
    for ref in statement.tables:
        try:
            info = engine.table_info(ref)
        except EngineError as exc:
            notes.append(f"{ref}: {exc}")
            continue
        if info is not None:
            infos[ref.lower()] = info
        else:
            notes.append(f"{ref}: no metadata found")
    return infos, notes


def _store(settings: Settings) -> Store:
    return Store(settings.state_dir)


# -- status ----------------------------------------------------------------------------
def status(settings: Settings) -> dict[str, Any]:
    spent = _store(settings).spent_today()
    budget = settings.budget
    return {
        "status": "ok",
        "version": __version__,
        "connections": [c.public_dict() for c in settings.connections.values()],
        "default_connection": settings.default_connection,
        "budget": budget.as_dict(),
        "spent_today": spent,
        "remaining_today_usd": round(max(0.0, budget.day_usd - spent["usd"]), 6),
        "workflow": WORKFLOW,
        "next_step": "Call gulliver_inspect to see which tables are big, then gulliver_preflight your SQL.",
    }


# -- inspect ---------------------------------------------------------------------------
def inspect(settings: Settings, connection: str | None = None, table: str | None = None,
            limit: int = 25) -> dict[str, Any]:
    try:
        with _engine(settings, connection) as (conn, engine):
            if table:
                info = engine.table_info(table)
                if info is None:
                    return _error(f"Table not found or no metadata: {table}",
                                  "Call gulliver_inspect without a table to list what exists.",
                                  connection=conn.name)
                return {"status": "ok", "connection": conn.name, "engine": conn.engine,
                        "table": info.as_dict(with_columns=True),
                        "next_step": "Write your SQL using only the columns you need, then call gulliver_preflight."}
            tables = engine.list_tables(limit=max(1, min(int(limit), 200)))
            return {"status": "ok", "connection": conn.name, "engine": conn.engine,
                    "tables": [t.as_dict() for t in tables],
                    "next_step": "Inspect one table for its columns, or call gulliver_preflight with your SQL."}
    except (EngineError, ConfigError) as exc:
        return _error(str(exc))


# -- lint ------------------------------------------------------------------------------
def lint_sql(sql: str, dialect: str = "duckdb") -> dict[str, Any]:
    statement = classify(sql, dialect)
    if statement.kind == "block" or statement.select is None:
        return {"status": "block", "statement": statement.public(), "lint": [],
                "next_step": "Send a single SELECT (or CREATE/INSERT ... SELECT)."}
    findings = lint(statement.select, dialect)
    counts = summarize(findings)
    state = "block" if counts["error"] else ("warn" if counts["warn"] else "ok")
    result = {"status": state, "statement": statement.public(), "lint": [f.as_dict() for f in findings],
              "counts": counts,
              "next_step": ("Fix the errors listed in lint." if counts["error"] else
                            "Call gulliver_preflight against a connection for cost and size.")}
    _attach_autofix(result, sql, dialect, None)
    return result


def _attach_autofix(result: dict[str, Any], sql: str, dialect: str, table_info: dict[str, dict] | None) -> None:
    """When a result-preserving rewrite exists, hand the agent the fixed SQL right away (no extra call)."""
    from .verify import quick_rewrite

    try:
        out = quick_rewrite(sql, dialect, table_info)  # the rewrite, plus a short test on generated data
    except Exception:  # an autofix hint must never break preflight or lint
        return
    if not out.get("changes"):
        return
    result["autofix"] = {"fixed_sql": out["fixed_sql"], "same_result": True,
                         "rules": sorted({c["rule"] for c in out["changes"]}),
                         "changes": out["changes"]}
    if out.get("verify"):
        result["autofix"]["verified"] = out["verify"]
    result["next_step"] = (result.get("next_step", "") + " autofix.fixed_sql returns exactly the same rows and avoids "
                           "the " + ", ".join(result["autofix"]["rules"]) + " problem; prefer it.").strip()


# -- preflight -------------------------------------------------------------------------
def _gate(settings: Settings, engine: Engine, estimate: Any, findings: list[Finding],
          spent_usd: float) -> dict[str, Any]:
    budget = settings.budget
    blocks: list[str] = []
    warnings: list[str] = []
    cost = estimate.cost_usd if estimate is not None else None
    bytes_ = estimate.bytes if estimate is not None else None

    errors = [f for f in findings if f.severity == "error"]
    if errors:
        blocks.append("scale lint errors: " + ", ".join(f"{f.rule} {f.name}" for f in errors))
    if cost is not None and cost > budget.max_query_usd:
        blocks.append(f"estimated cost {_fmt_usd(cost)} is over the per-query limit {_fmt_usd(budget.max_query_usd)}")
    if bytes_ is not None and bytes_ > budget.max_bytes:
        blocks.append(f"estimated scan {_fmt_bytes(bytes_)} is over the limit {_fmt_bytes(budget.max_bytes)}")
    if cost is not None and spent_usd + cost > budget.day_usd:
        blocks.append(f"today's budget: spent {_fmt_usd(spent_usd)} + {_fmt_usd(cost)} > {_fmt_usd(budget.day_usd)}")
    for f in findings:
        if f.severity == "warn":
            warnings.append(f"{f.rule} {f.name}")

    needs_rehearsal = False
    if cost is not None and engine.billed and cost > budget.rehearse_over_usd:
        needs_rehearsal = True
    if bytes_ is not None and bytes_ > budget.rehearse_over_bytes and (cost is None or not engine.billed):
        needs_rehearsal = True
    if engine.billed and cost is None and bytes_ is None:
        needs_rehearsal = True

    status = "block" if blocks else ("warn" if warnings else "ok")
    return {
        "status": status,
        "blocks": blocks,
        "warnings": warnings,
        "needs_rehearsal": needs_rehearsal,
        "needs_approval": bool(cost is not None and cost > budget.approval_over_usd),
    }


def _preflight_core(settings: Settings, sql: str, connection: str | None) -> dict[str, Any]:
    conn = settings.connection(connection)
    dialect = dialect_for(conn.engine)
    statement = classify(sql, dialect, policy_for(conn))
    base: dict[str, Any] = {
        "action": "preflight",
        "connection": conn.name,
        "engine": conn.engine,
        "sql": sql.strip(),
        "sql_hash": statement.sql_hash,
        "statement": statement.public(),
    }
    if statement.kind == "block":
        base.update(status="block", gate={"status": "block", "blocks": [statement.reason]},
                    next_step=statement.reason if statement.code else
                    "Rewrite it as one SELECT (or CREATE/INSERT ... SELECT). " + statement.reason)
        return base

    store = _store(settings)
    with _engine(settings, connection) as (_, engine):
        infos, table_notes = _table_infos(engine, statement)
        findings = lint(statement.select, dialect, {k: v.as_dict() for k, v in infos.items()})
        select_sql = statement.select.sql(dialect=dialect)
        try:
            estimate = engine.estimate(select_sql)
        except EngineError as exc:
            base.update(status="error", error=str(exc), lint=[f.as_dict() for f in findings],
                        tables=[t.as_dict() for t in infos.values()],
                        next_step="The engine rejected the SQL. Fix it (check table and column names with "
                                  "gulliver_inspect) and preflight again.")
            return base
        spent = store.spent_today()["usd"]
        gate = _gate(settings, engine, estimate, findings, spent)

    if gate["status"] == "block":
        next_step = "Do not run this. Fix: " + "; ".join(gate["blocks"]) + "."
    elif gate["needs_rehearsal"]:
        next_step = "Call gulliver_rehearse with the same SQL before gulliver_run (the scan is big)."
    elif gate["needs_approval"]:
        next_step = "Call gulliver_run; it will ask a human to approve because of the cost."
    else:
        next_step = "Within budget. You may call gulliver_run with the same SQL."
    base.update(
        status=gate["status"],
        tables=[t.as_dict() for t in infos.values()],
        estimate=estimate.as_dict(),
        lint=[f.as_dict() for f in findings],
        gate=gate,
        next_step=next_step,
    )
    if table_notes:
        base["notes"] = table_notes
    if findings:
        _attach_autofix(base, sql, dialect, {k: v.as_dict(with_columns=True) for k, v in infos.items()})
    return base


def preflight(settings: Settings, sql: str, connection: str | None = None) -> dict[str, Any]:
    try:
        result = _preflight_core(settings, sql, connection)
    except (EngineError, ConfigError) as exc:
        return _error(str(exc))
    _store(settings).save_receipt(result)
    return result


# -- rehearse --------------------------------------------------------------------------
def _choose_sample_tables(infos: dict[str, TableInfo], statement: Statement, mode: str) -> tuple[set[str] | None, int | None]:
    sized = [(ref, info) for ref, info in infos.items() if (info.rows or info.bytes)]
    largest_rows = max((info.rows or 0 for _, info in sized), default=None)
    if mode == "all" or not sized:
        return None, largest_rows
    ref, _ = max(sized, key=lambda item: ((item[1].rows or 0), (item[1].bytes or 0)))
    return {ref}, largest_rows


def rehearse(settings: Settings, sql: str, connection: str | None = None,
             fractions: list[float] | None = None, sample: str = "largest", method: str = "auto",
             seed: int = 42) -> dict[str, Any]:
    try:
        return _rehearse(settings, sql, connection, fractions, sample, method, seed)
    except (EngineError, ConfigError) as exc:
        return _error(str(exc))


def _rehearse(settings: Settings, sql: str, connection: str | None, fractions: list[float] | None,
              sample: str, method: str, seed: int) -> dict[str, Any]:
    budget = settings.budget
    store = _store(settings)
    conn = settings.connection(connection)
    dialect = dialect_for(conn.engine)
    statement = classify(sql, dialect, policy_for(conn))
    result: dict[str, Any] = {
        "action": "rehearse", "connection": conn.name, "engine": conn.engine, "sql": sql.strip(),
        "sql_hash": statement.sql_hash, "statement": statement.public(),
    }
    if statement.kind == "block":
        result.update(status="block", next_step=statement.reason)
        return result
    if sample not in ("largest", "all"):
        return _error("sample must be 'largest' or 'all'")
    fracs = sorted({float(f) for f in (fractions or DEFAULT_FRACTIONS)})
    if not fracs or any(not (0 < f < 1) for f in fracs):
        return _error("fractions must be between 0 and 1, e.g. [0.01, 0.03, 0.1]")

    with _engine(settings, connection) as (_, engine):
        infos, table_notes = _table_infos(engine, statement)
        findings = lint(statement.select, dialect, {k: v.as_dict() for k, v in infos.items()})
        select_sql = statement.select.sql(dialect=dialect)
        full_estimate = engine.estimate(select_sql)
        only, largest_rows = _choose_sample_tables(infos, statement, sample)

        if largest_rows is not None and largest_rows < SMALL_DATA_ROWS:
            result.update(status="skipped", reason=f"The data is small (largest table {largest_rows:,} rows). "
                                                    "A rehearsal adds nothing here.",
                          estimate=full_estimate.as_dict(),
                          next_step="Call gulliver_run directly (it is still budget-checked).")
            return result

        methods = engine.supported_methods()
        if method == "auto":
            chosen = "system" if ("bernoulli" not in methods or (largest_rows or 0) >= SYSTEM_SAMPLING_ROWS) else "bernoulli"
        elif method in methods:
            chosen = method
        else:
            return _error(f"method '{method}' is not supported by {engine.name}; use one of {', '.join(methods)}")

        baseline = engine.baseline_latency()
        rungs: list[dict[str, Any]] = []
        stopped: str | None = None
        spent = store.spent_today()["usd"]
        cap = engine.max_bytes_for_cost(budget.max_query_usd)

        for fraction in fracs:
            sampled_sql, sampled_tables = sample_query(
                statement.select, dialect, lambda ref, f=fraction: engine.sample_clause(f, seed, chosen), only)
            rung_cost = None
            if engine.billed:
                try:
                    rung_est = engine.estimate(sampled_sql)
                    rung_cost = rung_est.cost_usd
                except EngineError as exc:
                    stopped = f"the engine rejected the sampled query at {fraction:.0%}: {exc}"
                    break
            if rung_cost is None and len(rungs) >= 2:
                fit = fit_power_law("runtime_s", [r["fraction"] for r in rungs],
                                    [r["elapsed_s"] for r in rungs], target=fraction, offset=baseline)
                predicted_rt = fit.high or fit.value
                if predicted_rt is not None and predicted_rt > min(budget.max_runtime_s, REHEARSAL_RUNG_TIMEOUT_S):
                    stopped = (f"the {fraction:.0%} rung would take about {predicted_rt:.0f}s; stopped to stay "
                               "within the time limit")
                    break
                rung_cost = engine.cost_for(None, fit.value) if engine.billed else 0.0
            if rung_cost is not None and rung_cost > budget.max_query_usd:
                stopped = f"the {fraction:.0%} rung alone would cost {_fmt_usd(rung_cost)}"
                break
            if rung_cost is not None and spent + rung_cost > budget.day_usd:
                stopped = "today's budget would be exceeded"
                break
            try:
                res = engine.execute(sampled_sql, preview_rows=0,
                                     timeout_s=min(budget.max_runtime_s, REHEARSAL_RUNG_TIMEOUT_S),
                                     max_bytes_billed=cap)
            except EngineError as exc:
                stopped = f"the {fraction:.0%} rung failed: {exc}"
                break
            cost = res.cost_usd if res.cost_usd is not None else engine.cost_for(res.bytes_processed, res.elapsed_s)
            spent += cost or 0.0
            store.record_spend({"kind": "rehearsal", "connection": conn.name, "engine": conn.engine,
                                "sql_hash": statement.sql_hash, "fraction": fraction,
                                "cost_usd": cost, "bytes": res.bytes_processed, "elapsed_s": round(res.elapsed_s, 4)})
            rungs.append({"fraction": fraction, "elapsed_s": round(res.elapsed_s, 4), "output_rows": res.rows,
                          "bytes": res.bytes_processed, "cost_usd": cost, "sampled_tables": sampled_tables})

    if not rungs:
        result.update(status="error", error=f"No rung could run: {stopped}", lint=[f.as_dict() for f in findings],
                      next_step="Fix the SQL or lower the fractions, then rehearse again.")
        return result

    fs = [r["fraction"] for r in rungs]
    fits: list[Fit] = [
        fit_power_law("runtime_s", fs, [r["elapsed_s"] for r in rungs], offset=baseline),
        fit_power_law("output_rows", fs, [r["output_rows"] for r in rungs]),
    ]
    if any(r["bytes"] for r in rungs):
        fits.append(fit_power_law("bytes", fs, [r["bytes"] for r in rungs]))
    by_name = {f.metric: f for f in fits}
    runtime_fit = by_name["runtime_s"]
    if runtime_fit.confidence != "none" and all(r["elapsed_s"] - baseline < 0.05 for r in rungs):
        runtime_fit.confidence = "low"
        runtime_fit.notes.append("Every rung finished in under 50 ms above the overhead; runtime is a rough guess.")
    elif runtime_fit.confidence == "high" and max(r["elapsed_s"] for r in rungs) < 0.5:
        runtime_fit.confidence = "medium"
        runtime_fit.notes.append("Rungs were short (under 0.5 s), so fixed costs blur the curve; "
                                 "treat the runtime as a rough range.")

    # When the output grows faster than the runtime did, the full run still has to produce (and ship) every
    # row, so runtime follows the output. Re-predict from the last rung with the output exponent (safe side).
    rows_k = by_name["output_rows"].exponent
    if (rows_k is not None and runtime_fit.exponent is not None and rows_k > 1.0
            and rows_k > runtime_fit.exponent + 0.2 and runtime_fit.value is not None):
        last = rungs[-1]
        follow = baseline + max(last["elapsed_s"] - baseline, 0.0) * (1.0 / last["fraction"]) ** rows_k
        if follow > runtime_fit.value:
            scale = follow / runtime_fit.value
            runtime_fit.value = follow
            runtime_fit.low = (runtime_fit.low or follow) * scale
            runtime_fit.high = (runtime_fit.high or follow) * scale
            runtime_fit.exponent = rows_k
            runtime_fit.notes.append("Output grows faster than the measured runtime; runtime re-predicted to follow "
                                     "the output (every row still has to be produced).")

    # A GROUP BY result cannot have more rows than there are distinct keys, so group counts flatten out
    # (saturate) as the data grows. A power law misses that; cap it with what we observed and the planner.
    rows_fit = by_name["output_rows"]
    outer = _outer_select(statement.select)
    if outer is not None and _has_aggregation(outer) and rows_fit.value is not None:
        observed = max(r["output_rows"] for r in rungs)
        cap = max(observed, full_estimate.rows or 0)
        if cap and rows_fit.value > cap * 1.5:
            rows_fit.value = float(cap)
            rows_fit.low = float(observed)
            rows_fit.high = float(cap) * 2
            rows_fit.notes.append("Grouped result: group counts saturate, so the prediction is capped by the "
                                  "largest rung and the planner estimate.")

    predictions = {f.metric: f.as_dict() for f in fits}
    # money: exact dry-run bytes beat extrapolated bytes; otherwise use the engine's cost model
    cost_pred: dict[str, Any]
    if full_estimate.cost_usd is not None and full_estimate.accuracy == "exact":
        cost_pred = {"value": full_estimate.cost_usd, "low": full_estimate.cost_usd, "high": full_estimate.cost_usd,
                     "source": "full-query dry run (exact bytes)"}
    else:
        bytes_fit = by_name.get("bytes")
        cost_pred = {
            "value": engine.cost_for(int(bytes_fit.value) if bytes_fit and bytes_fit.value else None, runtime_fit.value),
            "low": engine.cost_for(int(bytes_fit.low) if bytes_fit and bytes_fit.low else None, runtime_fit.low),
            "high": engine.cost_for(int(bytes_fit.high) if bytes_fit and bytes_fit.high else None, runtime_fit.high),
            "source": "engine cost model on predicted runtime/bytes" if engine.billed else "local engine: no money",
        }
    predictions["cost_usd"] = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in cost_pred.items()}
    confidence = overall_confidence(fits)

    reasons: list[str] = []
    warn: list[str] = []
    if runtime_fit.value is not None and runtime_fit.value > budget.max_runtime_s:
        reasons.append(f"predicted runtime {runtime_fit.value:,.0f}s is over the limit {budget.max_runtime_s:,.0f}s")
    if cost_pred["value"] is not None and cost_pred["value"] > budget.max_query_usd:
        reasons.append(f"predicted cost {_fmt_usd(cost_pred['value'])} is over the limit {_fmt_usd(budget.max_query_usd)}")
    for fit in fits:
        if fit.exponent is not None and fit.exponent > 1.3 and fit.metric in ("runtime_s", "output_rows"):
            warn.append(f"{fit.metric} grows faster than the data (exponent {fit.exponent:.2f})")
    if confidence in ("low", "none"):
        warn.append("low confidence: treat the prediction as a rough range")
    if stopped:
        warn.append("ladder stopped early: " + stopped)
    out_rows = by_name["output_rows"].value
    if out_rows and out_rows > budget.max_output_rows:
        warn.append(f"the full result has about {out_rows:,.0f} rows; only {budget.max_output_rows} come back as a "
                    "preview. Aggregate if you need all of it.")

    state = "block" if reasons else ("warn" if warn else "ok")
    if state == "block":
        next_step = "Do not run the full query. Fix: " + "; ".join(reasons) + ". Narrow the filter or aggregate earlier."
    elif warn:
        next_step = "You may call gulliver_run, but read the warnings first (it is still gated)."
    else:
        next_step = "Prediction is within budget. Call gulliver_run with the same SQL."
    result.update(
        status=state,
        method=chosen,
        sample=sample,
        baseline_s=round(baseline, 4),
        rungs=rungs,
        prediction=predictions,
        confidence=confidence,
        estimate=full_estimate.as_dict(),
        lint=[f.as_dict() for f in findings],
        reasons=reasons,
        warnings=warn,
        next_step=next_step,
    )
    if table_notes:
        result["notes"] = table_notes
    store.save_receipt(result)
    return result


# -- run -------------------------------------------------------------------------------
def run(settings: Settings, sql: str, connection: str | None = None, max_rows: int | None = None,
        allow_write: bool = False) -> dict[str, Any]:
    try:
        return _run(settings, sql, connection, max_rows, allow_write)
    except (EngineError, ConfigError) as exc:
        return _error(str(exc))


def _run(settings: Settings, sql: str, connection: str | None, max_rows: int | None,
         allow_write: bool) -> dict[str, Any]:
    budget = settings.budget
    store = _store(settings)
    conn = settings.connection(connection)
    pre = _preflight_core(settings, sql, connection)
    pre["action"] = "run"
    statement_kind = pre.get("statement", {}).get("kind")

    def finish(status: str, next_step: str, **extra: Any) -> dict[str, Any]:
        pre.update(status=status, next_step=next_step, **extra)
        store.save_receipt(pre)
        return pre

    if pre["status"] == "error":
        return finish("error", pre["next_step"])
    if pre["status"] == "block":
        return finish("block", pre["next_step"])
    if statement_kind == "write":
        conn_allows = bool(conn.params.get("allow_write"))
        if not (allow_write and conn_allows):
            return finish("block", "This statement writes data. It needs allow_write=true on the call AND "
                                   "allow_write = true on the connection in gulliver.toml (set by a human).")

    gate = pre["gate"]
    estimate = pre.get("estimate") or {}
    est_cost = estimate.get("cost_usd")
    rehearsal = store.latest_rehearsal(pre["sql_hash"], conn.name)
    if gate.get("needs_rehearsal") and rehearsal is None:
        return finish("needs_rehearsal", "Call gulliver_rehearse with the same SQL first; this query is big enough "
                                         "that it must be rehearsed before the full run.")
    predicted_cost = None
    predicted_runtime = None
    if rehearsal:
        if rehearsal.get("status") == "block":
            return finish("block", "The last rehearsal predicted this is over budget: "
                          + "; ".join(rehearsal.get("reasons") or []) + ".", rehearsal_id=rehearsal.get("receipt_id"))
        pred = rehearsal.get("prediction") or {}
        predicted_cost = (pred.get("cost_usd") or {}).get("value")
        predicted_runtime = (pred.get("runtime_s") or {}).get("value")
        pre["rehearsal_id"] = rehearsal.get("receipt_id")

    cost_for_gate = max([c for c in (est_cost, predicted_cost) if c is not None], default=None)
    gate["cost_for_gate_usd"] = cost_for_gate
    if predicted_runtime is not None and predicted_runtime > budget.max_runtime_s:
        return finish("block", f"Predicted runtime {predicted_runtime:,.0f}s is over the limit {budget.max_runtime_s:,.0f}s.")
    if cost_for_gate is not None and cost_for_gate > budget.max_query_usd:
        return finish("block", f"Cost {_fmt_usd(cost_for_gate)} is over the per-query limit {_fmt_usd(budget.max_query_usd)}.")
    spent = store.spent_today()["usd"]
    if cost_for_gate is not None and spent + cost_for_gate > budget.day_usd:
        return finish("block", f"Today's budget would be exceeded (spent {_fmt_usd(spent)}, limit {_fmt_usd(budget.day_usd)}).")
    if cost_for_gate is not None and cost_for_gate > budget.approval_over_usd:
        approval = store.find_approval(pre["sql_hash"], conn.name)
        if approval is None:
            rid = store.new_id("run")
            pre["receipt_id"] = rid
            return finish("needs_approval",
                          f"Ask the human to approve: they run `gulliver approve {rid}` "
                          f"(cost {_fmt_usd(cost_for_gate)} is over the auto-approve limit "
                          f"{_fmt_usd(budget.approval_over_usd)}). Then call gulliver_run again.")
        pre["approval"] = approval

    preview_rows = max(0, min(int(max_rows) if max_rows is not None else budget.max_output_rows, budget.max_output_rows))
    write = statement_kind == "write"
    with _engine(settings, connection, read_only=not write) as (_, engine):
        cap = engine.max_bytes_for_cost(budget.max_query_usd)
        try:
            res = engine.execute(sql.strip().rstrip(";") if write else pre["sql"].rstrip(";"),
                                 preview_rows=preview_rows, timeout_s=budget.max_runtime_s, max_bytes_billed=cap)
        except EngineError as exc:
            return finish("error", "The run failed (nothing was returned). Read the error, fix the SQL, preflight again.",
                          error=str(exc))
        cost = res.cost_usd if res.cost_usd is not None else engine.cost_for(res.bytes_processed, res.elapsed_s)
    store.record_spend({"kind": "run", "connection": conn.name, "engine": conn.engine, "sql_hash": pre["sql_hash"],
                        "cost_usd": cost, "bytes": res.bytes_processed, "elapsed_s": round(res.elapsed_s, 4)})

    actual = {"runtime_s": round(res.elapsed_s, 4), "rows": res.rows, "bytes": res.bytes_processed,
              "cost_usd": round(cost, 6) if cost is not None else None}
    comparison: dict[str, Any] = {}
    if rehearsal:
        pred = rehearsal.get("prediction") or {}
        for key, metric in (("runtime_s", "runtime_s"), ("rows", "output_rows"), ("cost_usd", "cost_usd")):
            p = (pred.get(metric) or {}).get("value")
            a = actual.get(key)
            if p is not None and a is not None:
                comparison[key] = {"predicted": p, "actual": a,
                                   "ratio": round(a / p, 3) if p else None}
    result = finish("ok", "Done. Show the human the receipt (gulliver_receipt) if they want proof.",
                    actual=actual, columns=res.columns, preview=res.preview, truncated=res.truncated)
    if comparison:
        result["predicted_vs_actual"] = comparison
        store.save_receipt(result)
    if res.notes:
        result["notes"] = (result.get("notes") or []) + res.notes
    return result


# -- receipts and approvals ------------------------------------------------------------
def get_receipt(settings: Settings, receipt_id: str, fmt: str = "markdown") -> dict[str, Any]:
    from .receipts import render_markdown

    store = _store(settings)
    if receipt_id in ("latest", "last"):
        all_receipts = store.receipts()
        rec = all_receipts[0] if all_receipts else None
    else:
        rec = store.load_receipt(receipt_id)
    if rec is None:
        return _error(f"No receipt '{receipt_id}'.", "Use the receipt_id returned by preflight, rehearse or run.")
    if fmt == "json":
        return {"status": "ok", "receipt": rec, "next_step": "Share it with the human if they asked for proof."}
    return {"status": "ok", "receipt_id": rec.get("receipt_id"), "markdown": render_markdown(rec),
            "next_step": "Show this markdown to the human."}


def approve(settings: Settings, receipt_id: str, note: str = "") -> dict[str, Any]:
    store = _store(settings)
    try:
        approval = store.approve(receipt_id, note)
    except FileNotFoundError as exc:
        return _error(str(exc))
    return {"status": "ok", "approval": approval,
            "next_step": "The agent may now call gulliver_run with the same SQL (valid for 24 hours)."}
