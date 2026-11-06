"""Stage 2b rules on tiny hand-made profiles and keys: no database, no files."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import foreign_keys as fk  # noqa: E402
from profile_columns import quantile_emd  # noqa: E402

CFG = {**fk.DEFAULTS}


def col(name, position=0, distinct=10, rows=10, nulls=0, role="int", family="int", **extra):
    return {"name": name, "position": position, "rows": rows, "nulls": nulls, "null_rate": nulls / rows,
            "distinct": distinct, "uniqueness": 1.0, "role": role, "family": family, **extra}


def table(rows, **columns):
    return {"rows": rows, "columns": columns}


def pair(src, tgt, containment=1.0, coverage=1.0, source_unique=False, dist=1.0):
    """dist=1.0: the source's values spread over the whole target range; None: no distribution evidence."""
    return {"source": list(src), "target": list(tgt), "containment": containment, "coverage": coverage,
            "source_distinct": 10, "target_distinct": 10, "source_unique": source_unique, "intersection": 10,
            "dist": dist, "dist_buckets": None if dist is None else [1] * 10}


def key_table(pk, **columns):
    return {"primary_key": pk, "candidate_keys": [], "columns": columns}


def shop(extra_pairs=(), use_names=True):
    """orders.customer_id -> customers.customer_id, the textbook foreign key."""
    profile = {"db_id": "shop", "tables": {
        "customers": table(10, customer_id=col("customer_id", distinct=10, rows=10)),
        "orders": table(40, order_id=col("order_id", distinct=40, rows=40),
                        customer_id=col("customer_id", position=1, distinct=10, rows=40)),
    }, "containment": [pair(("orders", "customer_id"), ("customers", "customer_id")), *extra_pairs]}
    keys = {"use_names": use_names, "tables": {
        "customers": key_table("customer_id", customer_id={"score": 125.0}),
        "orders": key_table("order_id", order_id={"score": 100.0}, customer_id={"rejected": "has nulls"}),
    }}
    return profile, keys


def run(profile, keys, **cfg):
    return fk.score_database(profile, keys, {**CFG, **cfg})


def one(result, source, target):
    return next(r for r in result["candidates"] if (r["source"]["table"], r["source"]["column"]) == source
                and (r["target"]["table"], r["target"]["column"]) == target)


def test_textbook_fk_scores_full_marks_and_is_accepted():
    r = one(run(*shop()), ("orders", "customer_id"), ("customers", "customer_id"))
    # 25·1 + 15·1 + 20·1 + 15·min(40/10,2)/2 + 15·1 + 10·1 = 100 (distribution 1.0: spread over the whole target)
    assert r["score"] == 100.0 and r["decision"] == "accepted" and r["target_is_pk"]
    assert r["target_pk_score"] == 125.0 and r["cardinality"] == "many_to_one"


def test_g8_primary_key_is_never_a_source():
    profile, keys = shop([pair(("orders", "order_id"), ("customers", "customer_id"))])
    r = one(run(profile, keys), ("orders", "order_id"), ("customers", "customer_id"))
    assert r["decision"] == "rejected_gate" and r["reason"].startswith("G8")


def test_g8_spares_a_sparse_integer_key_unless_absolute():
    """A subclass table reusing its parent's key: scattered values (density 0.01) pass G8; numbered 1..n do not."""
    for density, gated in ((0.01, False), (0.5, False), (0.9, True)):
        profile, keys = shop([pair(("orders", "order_id"), ("customers", "customer_id"), source_unique=True)])
        profile["tables"]["orders"]["columns"]["order_id"]["numeric"] = {"density": density}
        r = one(run(profile, keys), ("orders", "order_id"), ("customers", "customer_id"))
        assert (r["decision"] == "rejected_gate" and r["reason"].startswith("G8")) == gated, density
    profile["tables"]["orders"]["columns"]["order_id"]["numeric"] = {"density": 0.01}
    r = one(run(profile, keys, pk_source_max_density=None), ("orders", "order_id"), ("customers", "customer_id"))
    assert r["reason"].startswith("G8")


def test_g1_target_rejected_by_2a_is_gated_out():
    profile, keys = shop([pair(("customers", "region_id"), ("orders", "customer_id"))])
    profile["tables"]["customers"]["columns"]["region_id"] = col("region_id", position=1)
    keys["tables"]["customers"]["columns"]["region_id"] = {"score": 10.0}
    r = one(run(profile, keys), ("customers", "region_id"), ("orders", "customer_id"))
    assert r["decision"] == "rejected_gate" and r["reason"].startswith("G1")


def test_same_table_excluded_unless_self_refs():
    profile, keys = shop()
    profile["tables"]["customers"]["columns"]["boss_id"] = col("boss_id", position=1)
    keys["tables"]["customers"]["columns"]["boss_id"] = {"score": 10.0}
    profile["containment"].append(pair(("customers", "boss_id"), ("customers", "customer_id")))
    assert one(run(profile, keys), ("customers", "boss_id"), ("customers", "customer_id"))["reason"] == "same table"
    assert one(run(profile, keys, self_refs=True), ("customers", "boss_id"), ("customers", "customer_id"))["decision"] != "rejected_gate"


def test_g6_containment_below_gate_is_rejected():
    profile, keys = shop()
    profile["containment"][0]["containment"] = 0.7
    assert one(run(profile, keys), ("orders", "customer_id"), ("customers", "customer_id"))["reason"].startswith("G6")


def test_prefilters():
    profile, keys = shop()
    profile["tables"]["orders"]["columns"]["customer_id"]["role"] = "boolean"
    assert "role boolean" in one(run(profile, keys), ("orders", "customer_id"), ("customers", "customer_id"))["reason"]
    profile, keys = shop()
    profile["tables"]["orders"]["columns"]["customer_id"]["distinct"] = 1
    assert "fewer than 2" in one(run(profile, keys), ("orders", "customer_id"), ("customers", "customer_id"))["reason"]
    profile, keys = shop()
    profile["tables"]["orders"]["columns"]["customer_id"]["text"] = {"avg_len": 80}
    assert "average length" in one(run(profile, keys), ("orders", "customer_id"), ("customers", "customer_id"))["reason"]


def test_orphan_penalty_applies_above_twenty_percent():
    profile, keys = shop()
    profile["containment"][0]["containment"] = 0.78       # orphan rate 0.22
    r = one(run(profile, keys), ("orders", "customer_id"), ("customers", "customer_id"))
    assert r["penalties"] == {"orphan": 0.7}
    # the overlap weight is 25 now (it was 40 before the distribution term took 15 of them)
    assert abs(r["score"] - 0.7 * (25 * 0.78 + 15 + 20 + 15 + 15 + 10)) < 1e-3


def test_no_names_moves_name_points_to_overlap():
    r = one(run(*shop(use_names=False)), ("orders", "customer_id"), ("customers", "customer_id"))
    assert "name" not in r["parts"] and r["score"] == 100.0       # 40·1 + 20·1 + 15 + 15 + 10
    assert run(*shop(use_names=False))["weights"] == {"overlap": 40, "distribution": 20, "name": 0, "cardinality": 15,
                                                      "target_key": 15, "nulls": 10}


def test_adaptive_weights_when_most_targets_are_not_the_chosen_key():
    profile, keys = shop()
    keys["tables"]["customers"]["primary_key"] = None            # k = 0 for every candidate
    profile["containment"][0]["containment"] = 0.8
    result = run(profile, keys)
    assert result["adaptive"] and result["weights"]["overlap"] == 40 and result["weights"]["target_key"] == 0
    assert result["weights"]["distribution"] == 15
    # 40·0.8 + 15·1 + 20 + 15 + 10 (the 15 target_key points moved to overlap: 25 -> 40)
    assert abs(one(result, ("orders", "customer_id"), ("customers", "customer_id"))["score"] - (40 * 0.8 + 15 + 20 + 15 + 10)) < 1e-3


def test_g5_keeps_three_targets_and_fan_out_scales_the_score():
    profile, keys = shop()
    for i in range(4):
        name = f"t{i}"
        profile["tables"][name] = table(10, id=col("id"))
        keys["tables"][name] = key_table("id", id={"score": 100.0})
        profile["containment"].append(pair(("orders", "customer_id"), (name, "id"), containment=0.9 - i * 0.01))
    result = run(profile, keys)
    kept = [r for r in result["candidates"] if r["source"]["column"] == "customer_id" and r["decision"] != "rejected_gate"]
    assert len(kept) == 3 and all(r["fan_out"] == 0.75 for r in kept)
    assert sum(r["reason"].startswith("G5") for r in result["candidates"] if r["decision"] == "rejected_gate" and "reason" in r) == 2


def test_decision_bands():
    assert fk.decide(59.9, CFG) == "rejected_score" and fk.decide(60, CFG) == "band"
    assert fk.decide(69.9, CFG) == "band" and fk.decide(70, CFG) == "accepted"


def test_graph_is_undirected_one_edge_per_pair():
    profile, keys = shop()
    profile["tables"]["customers"]["columns"]["customer_id"]["position"] = 0
    result = run(profile, keys)
    pairs = [frozenset(((e["source"]["table"], e["source"]["column"]), (e["target"]["table"], e["target"]["column"])))
             for e in result["graphs"]["stats"]]
    assert len(pairs) == len(set(pairs))
    assert result["graphs"]["direct"] == [e for e in result["graphs"]["stats"] if e["decision"] == "accepted"]


def test_name_similarity():
    assert fk.name_similarity("customer_id", "customers", "customer_id", CFG)[0] == 1.0
    assert fk.name_similarity("cust_id", "customers", "customer_id", CFG)[0] > 0.5
    assert fk.name_similarity("zzz", "customers", "customer_id", CFG)[0] < 0.4


def test_g9_is_off_by_default_and_still_works_when_set():
    """A 1-5 rating inside order ids 1..1000 is contained by chance; customer ids spanning the range are not."""
    assert fk.DEFAULTS["min_range_span"] is None
    for smax, gated in ((5, True), (900, False)):
        profile, keys = shop()
        profile["tables"]["orders"]["columns"]["customer_id"]["numeric"] = {"min": 1, "max": smax}
        profile["tables"]["customers"]["columns"]["customer_id"]["numeric"] = {"min": 1, "max": 1000}
        r = one(run(profile, keys, min_range_span=0.1), ("orders", "customer_id"), ("customers", "customer_id"))
        assert (r["decision"] == "rejected_gate" and r["reason"].startswith("G9")) == gated, smax
        assert one(run(profile, keys), ("orders", "customer_id"), ("customers", "customer_id"))["decision"] != "rejected_gate"


def test_quantile_emd_uniform_is_one_and_one_end_bucket_is_zero():
    assert quantile_emd([3] * 10) == 1.0 or abs(quantile_emd([3] * 10) - 1.0) < 1e-12
    assert quantile_emd([7] + [0] * 9) == 0.0          # EMD 4.5 / EMD_max 4.5
    assert quantile_emd([0] * 9 + [7]) == 0.0
    assert quantile_emd([0] * 10) is None                    # total == 0: no evidence
    assert quantile_emd([1, 1, 1, 1]) is None and quantile_emd([4]) is None      # L < 5 (a target with < 5 distinct values)
    assert abs(quantile_emd([2] * 5) - 1.0) < 1e-12 and abs(quantile_emd([9, 0, 0, 0, 0])) < 1e-12
    assert 0.0 < quantile_emd([5, 5, 5, 5, 5, 5, 5, 5, 0, 0]) < 1.0


def test_a_measure_in_a_corner_scores_low_distribution_and_a_lower_final_score():
    """Same containment, same everything else: only where the values sit over the target's range differs."""
    corner = quantile_emd([10] + [0] * 9)
    spread = quantile_emd([1] * 10)
    profile, keys = shop()
    profile["containment"] = [pair(("orders", "customer_id"), ("customers", "customer_id"), dist=spread)]
    full = one(run(profile, keys), ("orders", "customer_id"), ("customers", "customer_id"))
    profile["containment"] = [pair(("orders", "customer_id"), ("customers", "customer_id"), dist=corner)]
    low = one(run(profile, keys, dist_no_accept_below=None), ("orders", "customer_id"), ("customers", "customer_id"))   # cap off: pure weights
    assert low["parts"]["distribution"] == 0.0 and full["parts"]["distribution"] > 0.99
    assert low["score"] < full["score"] and abs((full["score"] - low["score"]) - 15 * full["parts"]["distribution"]) < 1e-3
    assert low["dist_buckets"] == [1] * 10          # kept from the pair for tracing


def test_few_distinct_source_values_in_the_first_bucket_score_dist_zero_are_capped_into_the_band_without_names():
    """A 1-3 measure contained by an id range: 10 buckets, all three values in the first, so dist = 0.

    Weights alone give 80 (40·1 + 0 + 15 + 15 + 10) and no fan-out for a single target, which would be
    auto-accepted; the cap (dist < 0.3) holds it at 69, in the LLM band."""
    chance = quantile_emd([3] + [0] * 9)
    assert abs(chance) < 1e-12
    profile, keys = shop(use_names=False)
    profile["tables"]["orders"]["columns"]["customer_id"]["distinct"] = 3
    profile["containment"] = [pair(("orders", "customer_id"), ("customers", "customer_id"), dist=chance)]
    uncapped = one(run(profile, keys, dist_no_accept_below=None), ("orders", "customer_id"), ("customers", "customer_id"))
    assert abs(uncapped["score"] - 80) < 1e-6 and uncapped["decision"] == "accepted" and "dist_capped" not in uncapped
    capped = one(run(profile, keys), ("orders", "customer_id"), ("customers", "customer_id"))
    assert capped["score"] == 69 and capped["decision"] == "band" and capped["dist_capped"] is True
    assert capped["score_before_fan_out"] == uncapped["score_before_fan_out"]       # the cap acts on the final score only


def test_cap_leaves_spread_sources_missing_evidence_and_already_lower_scores_alone():
    profile, keys = shop(use_names=False)
    key = (("orders", "customer_id"), ("customers", "customer_id"))
    # a spread source (dist >= 0.3) and a record with no distribution evidence are not capped
    for dist in (1.0, 0.3, None):
        profile["containment"] = [pair(*key, dist=dist)]
        r = one(run(profile, keys), *key)
        expected = 100.0 if dist is None else 80 + 20 * dist       # no names: 40·1 + 20·d + 15 + 15 + 10
        assert "dist_capped" not in r and abs(r["score"] - expected) < 1e-6 and r["decision"] == "accepted", dist
    # a score already below the cap is not lifted: two chance targets, fan-out 0.85 -> 68
    chance = quantile_emd([3] + [0] * 9)
    profile["tables"]["orders"]["columns"]["customer_id"]["distinct"] = 3
    profile["tables"]["cities"] = table(10, id=col("id"))
    keys["tables"]["cities"] = key_table("id", id={"score": 100.0})
    profile["containment"] = [pair(*key, dist=chance), pair(("orders", "customer_id"), ("cities", "id"), dist=chance)]
    both = run(profile, keys)
    for target in (("customers", "customer_id"), ("cities", "id")):
        r = one(both, ("orders", "customer_id"), target)
        assert abs(r["score"] - 68) < 1e-6 and r["decision"] == "band" and "dist_capped" not in r, r


def test_cap_threshold_and_flag():
    profile, keys = shop(use_names=False)
    key = (("orders", "customer_id"), ("customers", "customer_id"))
    profile["containment"] = [pair(*key, dist=0.2)]
    assert fk.DEFAULTS["dist_no_accept_below"] == 0.3
    assert one(run(profile, keys), *key)["score"] == 69                                      # 0.2 < 0.3
    assert one(run(profile, keys, dist_no_accept_below=0.1), *key)["score"] > 70             # threshold lowered
    assert one(run(profile, keys, dist_no_accept_below=None), *key)["score"] > 70            # cap disabled
    assert one(run(profile, keys, accept=50, delta=20), *key)["score"] == 69                 # accept + delta - 1
    ap = argparse.ArgumentParser()
    fk.add_args(ap)
    base = ["--dataset", "fiben"]
    assert fk.config_from(ap.parse_args(base))["dist_no_accept_below"] == 0.3
    assert fk.config_from(ap.parse_args(base + ["--dist-no-accept-below", "none"]))["dist_no_accept_below"] is None
    assert fk.config_from(ap.parse_args(base + ["--dist-no-accept-below", "-1"]))["dist_no_accept_below"] is None
    assert fk.config_from(ap.parse_args(base + ["--dist-no-accept-below", "0.45"]))["dist_no_accept_below"] == 0.45


def test_no_distribution_evidence_folds_its_points_into_overlap_for_that_record_only():
    profile, keys = shop([pair(("customers", "region_id"), ("orders", "order_id"), dist=0.4)])
    profile["tables"]["customers"]["columns"]["region_id"] = col("region_id", position=1)
    keys["tables"]["customers"]["columns"]["region_id"] = {"score": 10.0}
    profile["containment"][0]["dist"] = None
    profile["containment"][0]["dist_buckets"] = None
    result = run(profile, keys)
    none = one(result, ("orders", "customer_id"), ("customers", "customer_id"))
    seen = one(result, ("customers", "region_id"), ("orders", "order_id"))
    assert none["parts"]["distribution"] is None and none["weights_used"]["overlap"] == 40
    assert none["weights_used"]["distribution"] == 0 and none["score"] == 100.0       # same as the old 40-point overlap
    assert seen["parts"]["distribution"] == 0.4 and seen["weights_used"]["overlap"] == 25
    assert result["weights"]["distribution"] == 15                                      # the file-level weights are unchanged


def test_weights_override_replaces_both_tables():
    custom = {"overlap": 50, "distribution": 10, "name": 0, "cardinality": 15, "target_key": 15, "nulls": 10}
    for use_names in (True, False):
        assert fk.weights({**CFG, "weights": custom}, use_names, False) == custom
    assert fk.weights(CFG, True, False) == fk.W_NAMED and fk.weights(CFG, False, False) == fk.W_NO_NAMES


def test_weights_always_sum_to_100():
    for use_names in (True, False):
        for adaptive in (False, True):
            assert sum(fk.weights(CFG, use_names, adaptive).values()) == 100
    for use_names in (True, False):
        r = one(run(*shop(use_names=use_names)), ("orders", "customer_id"), ("customers", "customer_id"))
        assert sum(r["weights_used"].values()) == 100


def test_repair_adds_band_records_for_isolated_tables_only():
    a = col("a_key", family="text", role="code", distinct=100, rows=100)
    b = col("a_key", family="text", role="code", distinct=80, rows=200)
    dense = col("n", distinct=100, rows=100, numeric={"density": 1.0})
    tables = {"one": table(100, a_key=a, n=dense), "two": table(200, a_key=b, n=dense), "empty": table(0, x=col("x"))}
    profile = {"db_id": "d", "tables": tables, "containment": []}
    keys = {"use_names": True, "tables": {t: {"primary_key": None, "columns": {c: {} for c in tp["columns"]}}
                                          for t, tp in tables.items()}}
    overlaps = [{"a": ["one", "a_key"], "b": ["two", "a_key"], "a_distinct": 100, "b_distinct": 80, "shared": 20},
                {"a": ["one", "n"], "b": ["two", "n"], "a_distinct": 100, "b_distinct": 100, "shared": 100}]
    assert not fk.score_database(profile, keys, CFG, overlaps)["candidates"]            # off by default
    out = fk.score_database(profile, keys, {**CFG, "repair_isolated": 2}, overlaps)
    (r,) = out["candidates"]                                                            # dense ints left out
    assert r["decision"] == "band" and r["partial_overlap"] and r["source"]["table"] == "two"
    assert r["containment"] == 0.25 and len(out["graphs"]["stats"]) == 1 and not out["graphs"]["direct"]
