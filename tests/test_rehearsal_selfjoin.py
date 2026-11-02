"""Self-joins on a skewed, non-unique key: the prediction range must contain the true row count.

Regression for a bias where both occurrences of a self-joined table were sampled with the same seed, so they
held the same rows: every row met itself, small rungs came out too big, and the fit underestimated the full
join (the truth fell above the range every time).
"""

from __future__ import annotations

import duckdb
import pytest

from gulliver import core
from gulliver.config import load_settings

SELF_JOIN = "select a.id, b.id from t a join t b on a.k = b.k"
SEEDS = (1, 7, 42, 123)


@pytest.fixture(scope="module")
def skewed(tmp_path_factory):
    path = tmp_path_factory.mktemp("selfjoin") / "skewed.duckdb"
    con = duckdb.connect(str(path))
    # 30,000 rows: one hot key with 1,500 rows, the rest spread over 3,000 keys
    con.execute("""create table t as
                   select i as id, case when i % 20 = 0 then -1 else cast(hash(i) % 3000 as bigint) end as k
                   from range(30000) r(i)""")
    truth = con.execute("select sum(n * n) from (select k, count(*) as n from t group by k)").fetchone()[0]
    con.close()
    return str(path), int(truth)


def _inside(reh, truth):
    pred = reh["prediction"]["output_rows"]
    return pred["low"] <= truth <= pred["high"], pred


@pytest.mark.parametrize("seed", SEEDS)
def test_self_join_range_contains_truth(skewed, state, seed):
    path, truth = skewed
    reh = core.rehearse(load_settings(dsn=path), SELF_JOIN, seed=seed)
    ok, pred = _inside(reh, truth)
    assert ok, (truth, pred)
    assert 0.8 < pred["value"] / truth < 1.25
    assert reh["method"] == "bernoulli"
    assert any("multiplies rows" in w for w in reh["warnings"])


@pytest.mark.parametrize("seed", SEEDS)
def test_self_join_sample_all_range_contains_truth(skewed, state, seed):
    path, truth = skewed
    reh = core.rehearse(load_settings(dsn=path), SELF_JOIN, sample="all", seed=seed)
    ok, pred = _inside(reh, truth)
    assert ok, (truth, pred)
    assert 1.6 < pred["exponent"] < 2.5
    assert pred["confidence"] == "low"


def test_self_join_through_a_cte(skewed, state):
    path, truth = skewed
    sql = "with c as (select id, k from t) select a.id, b.id from c a join c b on a.k = b.k"
    for seed in (1, 42):
        ok, pred = _inside(core.rehearse(load_settings(dsn=path), sql, seed=seed), truth)
        assert ok, (truth, pred)
        assert 0.8 < pred["value"] / truth < 1.25


def test_semi_join_on_same_table(skewed, state):
    path, _ = skewed
    con = duckdb.connect(path, read_only=True)
    sql = "select id from t where k in (select k from t where id % 7 = 0)"
    truth = con.execute(f"select count(*) from ({sql})").fetchone()[0]
    con.close()
    ok, pred = _inside(core.rehearse(load_settings(dsn=path), sql, seed=42), truth)
    assert ok, (truth, pred)
