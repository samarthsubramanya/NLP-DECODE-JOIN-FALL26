"""Stage 1 distribution term (quantile-EMD) on a tiny in-memory DuckDB."""
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import profile_columns as pc  # noqa: E402


def test_bucket_expression_equals_duckdb_ntile():
    con = duckdb.connect()
    for rows in (10, 11, 37, 100, 1001):
        for n in (5, 7, 10):
            expected = con.execute(f"SELECT list(b ORDER BY rn) FROM (SELECT rn, ntile({n}) OVER (ORDER BY rn) AS b "
                                   f"FROM range(1, {rows + 1}) t(rn))").fetchone()[0]
            got = con.execute(f"SELECT list(b ORDER BY rn) FROM (SELECT rn, {pc.bucket_expr(rows, n)} AS b "
                              f"FROM range(1, {rows + 1}) t(rn))").fetchone()[0]
            assert got == expected, (rows, n)


def stage1_fixture():
    con = duckdb.connect()
    con.execute("CREATE TABLE parent (pid BIGINT, label VARCHAR)")
    con.execute("INSERT INTO parent SELECT i, 'p' || i FROM range(1, 1001) t(i)")
    con.execute("CREATE TABLE tiny (tid BIGINT)")                      # a target with 4 distinct values
    con.execute("INSERT INTO tiny VALUES (1), (2), (3), (4)")
    # fk draws from the whole parent key; rating sits in the corner 1..8; few has 3 distinct values, all low
    con.execute("CREATE TABLE child (fk BIGINT, rating BIGINT, few BIGINT)")
    con.execute("INSERT INTO child SELECT 1 + (i * 37) % 1000, 1 + i % 8, CASE WHEN i < 3 THEN i + 1 ELSE 3 END "
                "FROM range(0, 4000) t(i)")
    return pc.profile_database(con, "main", ["parent", "tiny", "child"], log=lambda *a: None)


def test_pairs_carry_dist_and_a_corner_measure_scores_low():
    prof = stage1_fixture()
    pairs = {tuple(p["source"]): p for p in prof["containment"] if p["target"] == ["parent", "pid"]}
    fk, rating, few = pairs[("child", "fk")], pairs[("child", "rating")], pairs[("child", "few")]
    assert len(fk["dist_buckets"]) == 10 and sum(fk["dist_buckets"]) == fk["intersection"]
    assert fk["dist"] > 0.95
    assert rating["dist"] is not None and rating["dist"] < 0.1 and rating["dist_buckets"][0] == 8


def test_bucket_count_is_not_capped_by_the_number_of_hits():
    """3 distinct source values, all in the first of 10 target buckets: 10 buckets, all mass in one end, dist 0."""
    few = next(p for p in stage1_fixture()["containment"]
               if p["source"] == ["child", "few"] and p["target"] == ["parent", "pid"])
    assert few["intersection"] == 3 and len(few["dist_buckets"]) == 10
    assert few["dist_buckets"] == [3] + [0] * 9
    assert few["dist"] is not None and abs(few["dist"]) < 1e-12


def test_a_target_with_fewer_than_five_distinct_values_gives_no_evidence_and_folds_into_overlap():
    import foreign_keys as fk
    prof = stage1_fixture()
    pair = next(p for p in prof["containment"] if p["source"] == ["child", "few"] and p["target"] == ["tiny", "tid"])
    assert pair["dist"] is None and pair["dist_buckets"] is None and pair["target_distinct"] == 4
    prof["containment"] = [pair]
    keys = {"use_names": False, "tables": {
        t: {"primary_key": pk, "columns": {c: {"score": 100.0} for c in prof["tables"][t]["columns"]}}
        for t, pk in (("parent", "pid"), ("tiny", "tid"), ("child", None))}}
    rec = fk.score_database(prof, keys)["candidates"][0]
    assert rec["parts"]["distribution"] is None
    assert rec["weights_used"]["distribution"] == 0 and rec["weights_used"]["overlap"] == 60     # 40 + 20
