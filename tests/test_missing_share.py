"""Missing share e = (nulls + placeholder rows) / rows, the (1 - e) key multiplier and the per-table fallback key."""
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import primary_keys as pk  # noqa: E402
import profile_columns as pc  # noqa: E402


def profile(sql, cap=0.5):
    con = duckdb.connect()
    con.execute(sql)
    return pc.profile_database(con, "main", ["t"], {**pc.DEFAULTS, "missing_cap": cap}, log=lambda *a: None)


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
def score(sql, cap=0.5):
    prof = profile(sql, cap)
    keys = pk.score_database(prof, {**pk.DEFAULTS, "use_names": False, "missing_cap": cap})
    return keys["tables"]["t"]["columns"]["k"]


def test_multiplier_is_one_minus_e_and_old_null_penalty_is_gone():
    assert "null_penalty" not in pk.DEFAULTS
    s = score("CREATE TABLE t AS SELECT CASE WHEN i < 2 THEN NULL ELSE i END AS k FROM range(10) r(i)")
    assert s["multipliers"]["missing"] == 0.8 and "nulls" not in s["multipliers"]


def test_no_multiplier_at_e_zero():
    s = score("CREATE TABLE t AS SELECT i AS k FROM range(10) r(i)")
    assert "missing" not in s["multipliers"]


def test_placeholders_are_penalised():
    s = score("CREATE TABLE t AS SELECT CASE WHEN i < 3 THEN 'N/A' ELSE 'v' || i END AS k FROM range(10) r(i)")
    assert abs(s["multipliers"]["missing"] - 0.7) < 1e-9
    assert s["parts"]["u"] == 1.0                                # 7 distinct real / 7 non-missing rows


# ------------------------------------------------------------------ the e rule (cap, uniqueness among non-missing, u)
def gated(sql, cap=0.5):
    prof = profile(sql, cap)
    return prof, pk.score_database(prof, {**pk.DEFAULTS, "use_names": False, "missing_cap": cap})


NULLS = "CREATE TABLE t AS SELECT CASE WHEN i < {n} THEN NULL ELSE i END AS k FROM range(10) r(i)"


def test_there_is_no_switch_and_no_dense_pick_or_old_null_path():
    for key in ("e_gate", "dense_pick", "dense_null_max", "dense_fallback", "reject_nulls", "null_penalty"):
        assert key not in pk.DEFAULTS and key not in pc.DEFAULTS, key
    assert pk.DEFAULTS["missing_cap"] == 0.5 and pc.DEFAULTS["missing_cap"] == 0.5
    assert not hasattr(pc, "mark_dense_picks")
    prof = profile(NULLS.format(n=3))
    assert "dense_pick" not in col(prof) and col(prof)["e_eligible"] is True


def test_e_under_cap_passes_over_cap_rejects():
    for n, cap, ok in ((2, 0.2, True), (3, 0.2, False), (6, 0.5, False), (5, 0.5, True), (9, None, True)):
        prof, keys = gated(NULLS.format(n=n), cap)
        assert pc.key_like(col(prof)) is ok, (n, cap)
        rej = keys["tables"]["t"]["columns"]["k"].get("rejected")
        assert (rej is None) is ok, (n, cap, rej)
        if not ok:
            assert rej.startswith("missing share e=") and "over cap" in rej


def test_placeholders_count_as_missing():
    sql = "CREATE TABLE t AS SELECT CASE WHEN i < 3 THEN 'N/A' ELSE 'v' || i END AS k FROM range(10) r(i)"
    assert not pc.key_like(col(gated(sql, 0.2)[0]))               # e = 0.3 > 0.2, although there is not one null
    assert pc.key_like(col(gated(sql, 0.5)[0]))


def test_uniqueness_is_among_non_missing_values_not_overall():
    prof, _ = gated("CREATE TABLE t AS SELECT CASE WHEN i < 2 THEN NULL WHEN i < 4 THEN '-' ELSE 'v' || i END AS k "
                    "FROM range(10) r(i)")
    p = col(prof)
    assert p["distinct"] != p["rows"] and pc.key_like(p)         # not unique overall, unique among non-missing
    prof, _ = gated("CREATE TABLE t AS SELECT CASE WHEN i < 2 THEN NULL ELSE i % 3 END AS k FROM range(10) r(i)")
    assert not pc.key_like(col(prof))
    prof, _ = gated("CREATE TABLE t AS SELECT NULL::BIGINT AS k FROM range(10) r(i)")
    assert not pc.key_like(col(prof))                             # no non-missing value


def test_missing_cap_none_removes_the_cap():
    assert pc.key_like(col(gated(NULLS.format(n=9), None)[0])) and not pc.key_like(col(gated(NULLS.format(n=9))[0]))


def test_u_is_one_for_a_fully_unique_column_with_placeholders_and_never_exceeds_one():
    _, keys = gated("CREATE TABLE t AS SELECT CASE WHEN i < 3 THEN 'N/A' ELSE 'v' || i END AS k FROM range(10) r(i)")
    assert keys["tables"]["t"]["columns"]["k"]["parts"]["u"] == 1.0
    for sql in (NULLS.format(n=4), "CREATE TABLE t AS SELECT i % 4 AS k FROM range(10) r(i)",
                "CREATE TABLE t AS SELECT CASE WHEN i < 2 THEN NULL WHEN i < 5 THEN '-' ELSE 'v' || i END AS k FROM range(10) r(i)"):
        assert gated(sql)[1]["tables"]["t"]["columns"]["k"]["parts"]["u"] <= 1.0


def test_u_equals_samarths_value_without_placeholders():
    for sql in (NULLS.format(n=4), "CREATE TABLE t AS SELECT i % 4 AS k FROM range(10) r(i)",
                "CREATE TABLE t AS SELECT CASE WHEN i < 3 THEN NULL ELSE i % 5 END AS k FROM range(10) r(i)"):
        prof, keys = gated(sql)
        p = col(prof)
        assert p["placeholder_rate"] == 0
        assert keys["tables"]["t"]["columns"]["k"]["parts"]["u"] == p["distinct"] / max(1, p["rows"] - p["nulls"])


def test_one_minus_e_multiplier_and_the_arithmetic():
    _, keys = gated(NULLS.format(n=4))
    s = keys["tables"]["t"]["columns"]["k"]
    assert abs(s["multipliers"]["missing"] - 0.6) < 1e-9
    m = 1.0
    for v in s["multipliers"].values():
        m *= v
    assert abs(s["score"] - round((s["base"] * m + s["boost"]) * s["phi"], 3)) < 1e-6


# ------------------------------------------------------------------ fallback marker
def test_fallback_marks_the_lowest_e_eligible_column_when_there_is_no_strict_key():
    p = profile("CREATE TABLE t AS SELECT CASE WHEN i < 6 THEN NULL ELSE i END AS a, "
                "CASE WHEN i < 3 THEN NULL ELSE i END AS b, i % 3 AS c FROM range(10) r(i)")
    # a: e = 0.6 over the cap; b: e = 0.3, unique among non-nulls; c repeats. No strict key in the table.
    assert "fallback_key" in col(p, "b") and pc.key_like(col(p, "b"))
    assert "fallback_key" not in col(p, "a") and "fallback_key" not in col(p, "c")
    assert not pc.key_like(col(p, "a"))                           # the marker does not lift the cap


def test_no_fallback_when_a_strict_key_exists():
    p = profile("CREATE TABLE t AS SELECT i AS id, CASE WHEN i < 3 THEN NULL ELSE i END AS k FROM range(10) r(i)")
    assert pc.strict_key_like(col(p, "id")) and "fallback_key" not in col(p, "k") and pc.key_like(col(p, "k"))


def test_no_fallback_when_nothing_passes_the_e_rule_and_it_can_be_switched_off():
    p = profile("CREATE TABLE t AS SELECT CASE WHEN i < 3 THEN NULL ELSE i % 4 END AS k FROM range(10) r(i)")
    assert "fallback_key" not in col(p)                           # repeats among non-null values
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT CASE WHEN i < 3 THEN NULL ELSE i END AS k FROM range(10) r(i)")
    p = pc.profile_database(con, "main", ["t"], {**pc.DEFAULTS, "missing_fallback": False}, log=lambda *a: None)
    assert "fallback_key" not in col(p) and pc.key_like(col(p))
