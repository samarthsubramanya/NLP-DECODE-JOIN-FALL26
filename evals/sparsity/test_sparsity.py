"""Checks for the sparsity sweep on tiny hand-made data: no benchmark files, no LLM.

    python -m pytest evals/sparsity -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401
import foreign_keys as fk  # noqa: E402

import adaptive  # noqa: E402
import arms as A  # noqa: E402
import margins as M  # noqa: E402


# ------------------------------------------------------------------ tiny profiles (same shape as tests/test_foreign_keys.py)
def col(name, position=0, distinct=10, rows=10, nulls=0, family="int", role="int"):
    return {"name": name, "position": position, "rows": rows, "nulls": nulls, "null_rate": nulls / rows,
            "distinct": distinct, "uniqueness": 1.0, "role": role, "family": family}


def pair(src, tgt, containment=1.0, dist=1.0):
    return {"source": list(src), "target": list(tgt), "containment": containment, "coverage": 1.0,
            "source_distinct": 10, "target_distinct": 10, "source_unique": False, "intersection": 10,
            "dist": dist, "dist_buckets": None if dist is None else [1] * 10}


def shop(containment=1.0, nulls=0, use_names=True):
    profile = {"db_id": "shop", "tables": {
        "customers": {"rows": 10, "columns": {"customer_id": col("customer_id")}},
        "orders": {"rows": 40, "columns": {"order_id": col("order_id", distinct=40, rows=40),
                                           "customer_id": col("customer_id", 1, distinct=10, rows=40, nulls=nulls)}},
    }, "containment": [pair(("orders", "customer_id"), ("customers", "customer_id"), containment)]}
    keys = {"use_names": use_names, "tables": {
        "customers": {"primary_key": "customer_id", "candidate_keys": [], "columns": {"customer_id": {"score": 125.0}}},
        "orders": {"primary_key": "order_id", "candidate_keys": [],
                   "columns": {"order_id": {"score": 100.0}, "customer_id": {"rejected": "has nulls"}}},
    }}
    return profile, keys


CFG = {**fk.DEFAULTS}


# ------------------------------------------------------------------ adaptive thresholds
def test_clean_signals_are_zero_and_leave_the_config_unchanged():
    profile, keys = shop()
    sig = adaptive.signals(profile, keys, CFG)
    assert sig["orphan_level"] == 0 and sig["null_level"] == 0
    out = adaptive.adapt(CFG, sig, ["all"])
    for k in ("containment_min", "orphan_max", "null_low", "null_high", "delta"):
        assert out[k] == CFG[k], k


def test_missing_parents_lower_tau_and_rescue_the_join():
    profile, keys = shop(containment=0.7)          # 30% of the FK's values lost their parent row
    sig = adaptive.signals(profile, keys, CFG)
    assert sig["orphan_level"] == pytest.approx(0.25)   # 0.3, capped at 0.25
    fixed = fk.score_database(profile, keys, CFG)
    adapted = fk.score_database(profile, keys, adaptive.adapt(CFG, sig, ["tau"]))
    assert fixed["candidates"][0]["decision"] == "rejected_gate"           # G6 at 0.75
    assert adapted["config"]["containment_min"] == pytest.approx(0.5)
    assert adapted["candidates"][0]["decision"] != "rejected_gate"


def test_null_rule_moves_the_null_term_cutoffs():
    profile, keys = shop(nulls=16)                 # 40% nulls in the FK source
    sig = adaptive.signals(profile, keys, CFG)
    assert sig["null_level"] == pytest.approx(0.4)
    out = adaptive.adapt(CFG, sig, ["null"])
    assert out["null_low"] == pytest.approx(0.7) and fk.null_term(0.4, out) == 1.0 and fk.null_term(0.4, CFG) == 0.5


def test_band_rule_widens_the_band_only_when_sparse():
    profile, keys = shop(nulls=16)
    out = adaptive.adapt(CFG, adaptive.signals(profile, keys, CFG), ["band"])
    assert out["delta"] == pytest.approx(CFG["delta"] * (1 + adaptive.BAND_GAIN * 0.4))


# ------------------------------------------------------------------ arms
def test_full_arm_is_the_unchanged_scorer():
    profile, keys = shop()
    a = A.run_arm("full", profile, keys, CFG, True)
    b = fk.score_database(profile, keys, CFG)
    assert a["candidates"] == b["candidates"] and a["graphs"] == b["graphs"]


@pytest.mark.parametrize("term", ["name", "cardinality", "target_key", "nulls"])
def test_dropping_a_term_keeps_100_points(term):
    profile, keys = shop()
    _, _, cfg = A.drop_term(term)(profile, keys, CFG, True)
    assert cfg["weights"][term] == 0 and sum(cfg["weights"].values()) == pytest.approx(100)


def test_name_arm_does_not_apply_without_names():
    profile, keys = shop(use_names=False)
    assert A.run_arm("no_name", profile, keys, CFG, False) is None


def test_no_qmd_removes_distribution_evidence_without_touching_the_input():
    profile, keys = shop()
    out = A.run_arm("no_qmd", profile, keys, CFG, True)
    assert out["candidates"][0]["parts"]["distribution"] is None
    assert profile["containment"][0]["dist"] == 1.0


def test_every_arm_runs():
    profile, keys = shop()
    for name in A.ARMS:
        A.run_arm(name, profile, keys, CFG, True)


# ------------------------------------------------------------------ margins
def test_margins_and_summary():
    profile, keys = shop()
    joins = fk.score_database(profile, keys, CFG)
    rec = joins["candidates"][0]
    m = M.margins(rec, joins["config"])
    assert m["accept"] == pytest.approx(rec["score"] - 60) and m["auto"] == pytest.approx(rec["score"] - 70)
    assert m["tau"] == pytest.approx(0.25)

    def norm(e):
        return frozenset(((e["source"]["table"], e["source"]["column"]), (e["target"]["table"], e["target"]["column"])))
    gold = {norm(rec)}
    rows = M.rows_for("shop", joins, gold, norm, joins["config"])
    s = M.summarize(rows, ("direct", "stats"))
    assert s["direct"] == {"tp": 1} and s["stats"] == {"tp": 1}
    s = M.summarize(M.rows_for("shop", joins, set(), norm, joins["config"]), ("direct",))
    assert s["direct"]["fp"] == 1 and s["direct"]["fp_far"] == 1     # 100 points: far above the auto line


# ------------------------------------------------------------------ sparsify (needs duckdb, no benchmark files)
def test_copy_table_rates_and_determinism(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    import sparsify as S
    src = tmp_path / "src.duckdb"
    c = duckdb.connect(str(src))
    c.execute("CREATE SCHEMA s; CREATE TABLE s.t AS SELECT i AS id, i % 7 AS v FROM range(2000) r(i);"
              "CREATE TABLE s.one AS SELECT 1 AS id")
    c.close()

    def make(name, spec, seed=0, table="t"):
        con = duckdb.connect(str(tmp_path / name))
        con.execute(f"ATTACH '{src}' AS src (READ_ONLY); CREATE SCHEMA s")
        S.copy_table(con, "s", table, ["id", "v"] if table == "t" else ["id"], spec, seed)
        rows = con.execute(f"SELECT * FROM s.{table} ORDER BY id").fetchall()
        con.close()
        return rows

    full = make("a.duckdb", {"keep": None, "null": {}})
    assert len(full) == 2000 and full[0] == (0, 0)
    kept = make("b.duckdb", {"keep": 0.3, "null": {}})
    assert len(kept) == 600
    assert kept == make("c.duckdb", {"keep": 0.3, "null": {}})                    # same seed, same rows
    assert kept != make("d.duckdb", {"keep": 0.3, "null": {}}, seed=1)
    nulled = make("e.duckdb", {"keep": None, "null": {"v": 0.5}})
    share = sum(v is None for _, v in nulled) / len(nulled)
    assert 0.45 < share < 0.55 and all(i is not None for i, _ in nulled)
    both = make("f.duckdb", {"keep": 0.3, "null": {"v": 0.5}})
    assert [i for i, _ in both] == [i for i, _ in kept]                            # nulls do not change which rows stay
    assert make("g.duckdb", {"keep": 0.01, "null": {}}, table="one") == [(1,)]      # a table keeps at least one row


def test_content_alignment_matches_across_renamed_and_reordered_copies(tmp_path):
    """make_variants.py: documented and obfuscated copies (other names, other row order) lose the same rows and cells."""
    duckdb = pytest.importorskip("duckdb")
    import sparsify as S
    src = tmp_path / "src.duckdb"
    c = duckdb.connect(str(src))
    c.execute("CREATE SCHEMA s; CREATE TABLE s.t AS SELECT i AS id, i % 7 AS v, 'x' || i AS w FROM range(3000) r(i);"
              "CREATE TABLE s.o AS SELECT id AS col_001, v AS col_002, w AS col_003 FROM s.t ORDER BY random()")
    c.close()
    spec = {"keep": 0.5, "null": {}}
    con = duckdb.connect(str(tmp_path / "dst.duckdb"))
    con.execute(f"ATTACH '{src}' AS src (READ_ONLY); CREATE SCHEMA s")
    S.copy_table(con, "s", "t", ["id", "v", "w"], {**spec, "null": {"id": 0.3, "v": 0.3, "w": 0.3}}, 0, "content")
    rows_t = con.execute("SELECT * FROM s.t").fetchall()
    con.execute("ALTER TABLE s.t RENAME TO t_doc")
    con.close()
    # the "obfuscated" source: same table name, renamed columns, shuffled rows
    c = duckdb.connect(str(src))
    c.execute("ALTER TABLE s.t RENAME TO t_orig; ALTER TABLE s.o RENAME TO t")
    c.close()
    con = duckdb.connect(str(tmp_path / "dst.duckdb"))
    con.execute(f"ATTACH '{src}' AS src (READ_ONLY)")
    S.copy_table(con, "s", "t", ["col_001", "col_002", "col_003"],
                 {**spec, "null": {"col_001": 0.3, "col_002": 0.3, "col_003": 0.3}}, 0, "content")
    rows_o = con.execute("SELECT * FROM s.t").fetchall()
    con.close()
    assert len(rows_t) == 1500 and sorted(rows_t, key=str) == sorted(rows_o, key=str)
    assert 0.25 < sum(r[1] is None for r in rows_t) / len(rows_t) < 0.35
