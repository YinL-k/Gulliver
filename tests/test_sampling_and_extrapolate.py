import math

import duckdb

from gulliver.extrapolate import fit_power_law
from gulliver.sampling import percent_text, sample_query
from gulliver.sqlsafety import classify


def clause(ref, occurrence=0):
    return f"TABLESAMPLE 10% (bernoulli, {42 + occurrence})"


def test_percent_text():
    assert percent_text(0.01) == "1"
    assert percent_text(0.005) == "0.5"
    assert percent_text(0.1) == "10"


def test_sample_rewrite_keeps_aliases_and_skips_ctes():
    stmt = classify("with big as (select * from events) select e.id from big join events e on big.id = e.id")
    sql, sampled = sample_query(stmt.select, "duckdb", clause)
    assert sampled == ["events"]
    assert "(SELECT * FROM events TABLESAMPLE 10% (bernoulli, 42)) AS e" in sql
    assert "FROM big" in sql or "FROM big " in sql


def test_sample_only_selected_tables():
    stmt = classify("select e.id from events e join users u on e.user_id = u.user_id")
    sql, sampled = sample_query(stmt.select, "duckdb", clause, only={"events"})
    assert sampled == ["events"]
    assert "JOIN users AS u" in sql


def test_self_join_occurrences_get_their_own_sample():
    stmt = classify("select a.id from events a join events b on a.user_id = b.user_id")
    sql, sampled = sample_query(stmt.select, "duckdb", clause)
    assert sampled == ["events"]
    assert "(SELECT * FROM events TABLESAMPLE 10% (bernoulli, 42)) AS a" in sql
    assert "(SELECT * FROM events TABLESAMPLE 10% (bernoulli, 43)) AS b" in sql
    sql, _ = sample_query(stmt.select, "duckdb", clause, first_only=True)
    assert "(SELECT * FROM events TABLESAMPLE 10% (bernoulli, 42)) AS a" in sql
    assert "JOIN events AS b" in sql


def test_cte_used_twice_is_split_per_use():
    stmt = classify("with e as (select * from events), f as (select * from e where id > 1) "
                    "select * from f a join f b on a.user_id = b.user_id join users u on u.user_id = a.user_id")
    sql, _ = sample_query(stmt.select, "duckdb", clause, only={"events"}, first_only=True)
    con = duckdb.connect()
    con.execute("create table events as select i as id, i % 10 as user_id from range(100) t(i)")
    con.execute("create table users as select i as user_id from range(10) t(i)")
    con.execute(sql)  # still valid SQL
    assert sql.count("TABLESAMPLE") == 1 and "e_gv1" in sql and "f_gv1 AS b" in sql
    sql, _ = sample_query(stmt.select, "duckdb", clause, only={"events"})
    assert "(bernoulli, 42)" in sql and "(bernoulli, 43)" in sql


def test_sampled_query_runs_in_duckdb():
    con = duckdb.connect()
    con.execute("create table events as select i as id, i % 10 as user_id from range(100000) t(i)")
    stmt = classify("select count(*) from events where user_id = 3")
    sql, _ = sample_query(stmt.select, "duckdb", clause)
    n = con.execute(sql).fetchone()[0]
    assert 500 < n < 1500  # about 10% of 10,000


def test_fit_linear():
    fit = fit_power_law("rows", [0.01, 0.03, 0.1], [100, 300, 1000])
    assert math.isclose(fit.value, 10_000, rel_tol=1e-6)
    assert math.isclose(fit.exponent, 1.0, abs_tol=1e-6)
    assert fit.confidence == "high"


def test_fit_quadratic_warns():
    fit = fit_power_law("output_rows", [0.01, 0.03, 0.1], [1, 9, 100])
    assert math.isclose(fit.exponent, 2.0, abs_tol=1e-6)
    assert math.isclose(fit.value, 10_000, rel_tol=1e-6)
    assert fit.confidence == "medium"
    assert any("faster than the data" in n for n in fit.notes)


def test_fit_flat_aggregate():
    fit = fit_power_law("output_rows", [0.01, 0.03, 0.1], [8, 8, 8])
    assert math.isclose(fit.value, 8)
    assert fit.confidence == "high"


def test_fit_with_overhead():
    # runtime = 0.5s overhead + 10s * f
    fit = fit_power_law("runtime_s", [0.01, 0.03, 0.1], [0.6, 0.8, 1.5], offset=0.5)
    assert math.isclose(fit.value, 10.5, rel_tol=1e-6)


def test_fit_one_point_and_none():
    one = fit_power_law("rows", [0.1], [50])
    assert one.value == 500 and one.confidence == "low"
    none = fit_power_law("rows", [0.1], [None])
    assert none.value is None and none.confidence == "none"
