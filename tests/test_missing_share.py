"""Missing share e = (nulls + placeholder rows) / rows, the (1 - e) key multiplier and the per-table fallback key."""
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import primary_keys as pk  # noqa: E402
import profile_columns as pc  # noqa: E402


def profile(sql):
    con = duckdb.connect()
    con.execute(sql)
    return pc.profile_database(con, "main", ["t"], log=lambda *a: None)


def col(prof, name="k"):
    return prof["tables"]["t"]["columns"][name]


# ------------------------------------------------------------------ e
def test_e_nulls_only():
    p = col(profile("CREATE TABLE t AS SELECT CASE WHEN i < 2 THEN NULL ELSE 'v' || i END AS k FROM range(10) r(i)"))
    assert p["nulls"] == 2 and p["placeholder_rate"] == 0 and p["missing_share"] == 0.2


def test_e_placeholders_only():
    p = col(profile("CREATE TABLE t AS SELECT CASE WHEN i < 3 THEN 'N/A' ELSE 'v' || i END AS k FROM range(10) r(i)"))
    assert p["nulls"] == 0 and p["placeholder_rate"] == 0.3 and p["missing_share"] == 0.3
    assert p["distinct_real"] == 7 and p["distinct"] == 8        # distinct still counts the placeholder value


def test_e_nulls_and_placeholders():
    p = col(profile("CREATE TABLE t AS SELECT CASE WHEN i < 2 THEN NULL WHEN i < 5 THEN '-' ELSE 'v' || i END AS k "
                    "FROM range(10) r(i)"))
    assert p["nulls"] == 2 and p["missing_share"] == 0.5


def test_e_no_missing_and_empty_table():
    p = col(profile("CREATE TABLE t AS SELECT 'v' || i AS k FROM range(10) r(i)"))
    assert p["missing_share"] == 0
    p = col(profile("CREATE TABLE t (k VARCHAR)"))
    assert p["rows"] == 0 and p["missing_share"] == 1.0
    assert pk.score_database(profile("CREATE TABLE t (k VARCHAR)"))["tables"]["t"]["columns"]["k"]["rejected"] == "empty table"


# ------------------------------------------------------------------ (1 - e) multiplier
def score(sql, **cfg):
    prof = profile(sql)
    keys = pk.score_database(prof, {**pk.DEFAULTS, "use_names": False, **cfg})
    return keys["tables"]["t"]["columns"]["k"]


def test_multiplier_is_one_minus_e_and_old_null_penalty_is_gone():
    assert "null_penalty" not in pk.DEFAULTS
    s = score("CREATE TABLE t AS SELECT CASE WHEN i < 2 THEN NULL ELSE i END AS k FROM range(10) r(i)")
    assert s["multipliers"]["missing"] == 0.8 and "nulls" not in s["multipliers"]


def test_no_multiplier_at_e_zero():
    s = score("CREATE TABLE t AS SELECT i AS k FROM range(10) r(i)")
    assert "missing" not in s["multipliers"]


def test_placeholders_are_penalised_and_u_is_clamped():
    s = score("CREATE TABLE t AS SELECT CASE WHEN i < 3 THEN 'N/A' ELSE 'v' || i END AS k FROM range(10) r(i)")
    assert abs(s["multipliers"]["missing"] - 0.7) < 1e-9
    assert s["parts"]["u"] == 1.0                                # 8 distinct / 7 non-missing rows, clamped


def test_missing_cap_is_off_by_default_and_rejects_when_set():
    assert pk.DEFAULTS["missing_cap"] == 0.5 and pc.DEFAULTS["missing_cap"] == 0.5 and not pk.DEFAULTS["e_gate"]
    sql = "CREATE TABLE t AS SELECT CASE WHEN i < 6 THEN NULL ELSE i END AS k FROM range(10) r(i)"   # e = 0.6
    assert "score" in score(sql)                                  # gate off: the cap is not applied
    assert gated(sql, 0.5)[1]["tables"]["t"]["columns"]["k"]["rejected"].startswith("missing share e=")
    assert "score" in gated(sql, None)[1]["tables"]["t"]["columns"]["k"]


# ------------------------------------------------------------------ fallback key
NULLY = ("CREATE TABLE t AS SELECT CASE WHEN i < 6 THEN NULL ELSE i END AS k, i % 2 AS dup FROM range(10) r(i)")


def test_fallback_fires_when_no_strict_key():
    p = profile(NULLY)                                           # k: 60% nulls, unique among non-null; dup repeats
    assert col(p)["fallback_key"] is True and pc.key_like(col(p)) and not pc.strict_key_like(col(p))
    assert pk.score_database(p, {**pk.DEFAULTS, "use_names": False})["tables"]["t"]["columns"]["k"].get("rejected") is None


def test_fallback_off_by_config():
    con = duckdb.connect()
    con.execute(NULLY)
    p = pc.profile_database(con, "main", ["t"], {**pc.DEFAULTS, "missing_fallback": False}, log=lambda *a: None)
    assert "fallback_key" not in col(p) and not pc.key_like(col(p))


def test_no_fallback_when_a_strict_key_exists():
    p = profile("CREATE TABLE t AS SELECT i AS id, CASE WHEN i < 6 THEN NULL ELSE i END AS k FROM range(10) r(i)")
    assert pc.strict_key_like(col(p, "id")) and "fallback_key" not in col(p, "k")


def test_fallback_needs_uniqueness_among_non_missing_and_picks_lowest_e():
    p = profile("CREATE TABLE t AS SELECT CASE WHEN i < 6 THEN NULL ELSE i END AS a, "
                "CASE WHEN i < 3 THEN NULL ELSE i END AS b, i % 3 AS c FROM range(10) r(i)")
    assert "fallback_key" in col(p, "b") and "fallback_key" not in col(p, "a") and "fallback_key" not in col(p, "c")
    p = profile("CREATE TABLE t AS SELECT CASE WHEN i < 3 THEN NULL ELSE i % 4 END AS d FROM range(10) r(i)")
    assert "fallback_key" not in col(p, "d")                     # repeats among non-null values


# ------------------------------------------------------------------ e_gate (one shared rule, u from distinct_real)
def gated(sql, cap=None, table="t"):
    con = duckdb.connect()
    con.execute(sql)
    cfg = {**pc.DEFAULTS, "e_gate": True, "missing_cap": cap}
    prof = pc.profile_database(con, "main", [table], cfg, log=lambda *a: None)
    keys = pk.score_database(prof, {**pk.DEFAULTS, "use_names": False, "e_gate": True, "missing_cap": cap})
    return prof, keys


NULLS20 = "CREATE TABLE t AS SELECT CASE WHEN i < {n} THEN NULL ELSE i END AS k FROM range(10) r(i)"


def test_e_gate_under_cap_passes_over_cap_rejects():
    for n, cap, ok in ((2, 0.2, True), (3, 0.2, False), (6, 0.5, False), (5, 0.5, True), (9, None, True)):
        prof, keys = gated(NULLS20.format(n=n), cap)
        assert pc.key_like(col(prof)) is ok, (n, cap)
        rej = keys["tables"]["t"]["columns"]["k"].get("rejected")
        assert (rej is None) is ok, (n, cap, rej)
        if not ok:
            assert rej.startswith("missing share e=") and "over cap" in rej


def test_e_gate_replaces_dense_pick_and_counts_placeholders_as_missing():
    sql = "CREATE TABLE t AS SELECT CASE WHEN i < 3 THEN 'N/A' ELSE 'v' || i END AS k FROM range(10) r(i)"
    assert pc.key_like(col(profile(sql)))                         # old rule: no nulls, so a dense pick passes
    assert not pc.key_like(col(gated(sql, 0.2)[0]))               # e = 0.3 > 0.2: placeholders count as missing
    assert pc.key_like(col(gated(sql, 0.5)[0]))


def test_e_gate_needs_uniqueness_among_non_missing_not_overall():
    prof, _ = gated("CREATE TABLE t AS SELECT CASE WHEN i < 2 THEN NULL WHEN i < 4 THEN '-' ELSE 'v' || i END AS k "
                    "FROM range(10) r(i)")
    p = col(prof)
    assert p["distinct"] != p["rows"] and pc.key_like(p)         # not unique overall, unique among non-missing
    prof, _ = gated("CREATE TABLE t AS SELECT CASE WHEN i < 2 THEN NULL ELSE i % 3 END AS k FROM range(10) r(i)")
    assert not pc.key_like(col(prof))
    prof, _ = gated("CREATE TABLE t AS SELECT NULL::BIGINT AS k FROM range(10) r(i)")
    assert not pc.key_like(col(prof))                             # no non-missing value


def test_u_is_one_for_a_fully_unique_column_with_placeholders_and_never_exceeds_one():
    _, keys = gated("CREATE TABLE t AS SELECT CASE WHEN i < 3 THEN 'N/A' ELSE 'v' || i END AS k FROM range(10) r(i)")
    assert keys["tables"]["t"]["columns"]["k"]["parts"]["u"] == 1.0
    for sql in (NULLS20.format(n=4), "CREATE TABLE t AS SELECT i % 4 AS k FROM range(10) r(i)",
                "CREATE TABLE t AS SELECT CASE WHEN i < 2 THEN NULL WHEN i < 5 THEN '-' ELSE 'v' || i END AS k FROM range(10) r(i)"):
        s = gated(sql)[1]["tables"]["t"]["columns"]["k"]
        assert s["parts"]["u"] <= 1.0


def test_u_equals_samarths_value_without_placeholders():
    for sql in (NULLS20.format(n=4), "CREATE TABLE t AS SELECT i % 4 AS k FROM range(10) r(i)",
                "CREATE TABLE t AS SELECT CASE WHEN i < 3 THEN NULL ELSE i % 5 END AS k FROM range(10) r(i)"):
        prof, keys = gated(sql)
        p = col(prof)
        assert p["placeholder_rate"] == 0
        assert keys["tables"]["t"]["columns"]["k"]["parts"]["u"] == p["distinct"] / max(1, p["rows"] - p["nulls"])


def test_e_gate_keeps_the_one_minus_e_multiplier_and_the_arithmetic():
    _, keys = gated(NULLS20.format(n=4))
    s = keys["tables"]["t"]["columns"]["k"]
    assert abs(s["multipliers"]["missing"] - 0.6) < 1e-9
    m = 1.0
    for v in s["multipliers"].values():
        m *= v
    assert abs(s["score"] - round((s["base"] * m + s["boost"]) * s["phi"], 3)) < 1e-6


def test_flag_off_is_the_old_behaviour():
    # 40% nulls, above dense_null_max 0.2; the strict key id keeps the per-table fallback out of it
    sql = "CREATE TABLE t AS SELECT i AS id, CASE WHEN i < 4 THEN NULL ELSE i END AS k FROM range(10) r(i)"
    prof = profile(sql)
    assert "e_eligible" not in col(prof) and not pc.key_like(col(prof))
    assert pk.score_database(prof, {**pk.DEFAULTS, "use_names": False})["tables"]["t"]["columns"]["k"]["rejected"] == "has nulls"
    sql = "CREATE TABLE t AS SELECT CASE WHEN i < 3 THEN 'N/A' ELSE 'v' || i END AS k FROM range(10) r(i)"
    s = score(sql)                                               # old: distinct / present clamped at 1
    assert s["parts"]["u"] == 1.0
