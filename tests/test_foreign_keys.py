"""Stage 2b rules on tiny hand-made profiles and keys: no database, no files."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import foreign_keys as fk  # noqa: E402

CFG = {**fk.DEFAULTS}


def col(name, position=0, distinct=10, rows=10, nulls=0, role="int", family="int", **extra):
    return {"name": name, "position": position, "rows": rows, "nulls": nulls, "null_rate": nulls / rows,
            "distinct": distinct, "uniqueness": 1.0, "role": role, "family": family, **extra}


def table(rows, **columns):
    return {"rows": rows, "columns": columns}


def pair(src, tgt, containment=1.0, coverage=1.0, source_unique=False):
    return {"source": list(src), "target": list(tgt), "containment": containment, "coverage": coverage,
            "source_distinct": 10, "target_distinct": 10, "source_unique": source_unique, "intersection": 10}


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
    # 40·1 + 20·1 + 15·min(40/10,2)/2 + 15·1 + 10·1 = 100
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
    assert abs(r["score"] - 0.7 * (40 * 0.78 + 20 + 15 + 15 + 10)) < 1e-3


def test_no_names_moves_name_points_to_overlap():
    r = one(run(*shop(use_names=False)), ("orders", "customer_id"), ("customers", "customer_id"))
    assert "name" not in r["parts"] and r["score"] == 100.0       # 60·1 + 15 + 15 + 10
    assert run(*shop(use_names=False))["weights"] == {"overlap": 60, "cardinality": 15, "target_key": 15, "nulls": 10}


def test_adaptive_weights_when_most_targets_are_not_the_chosen_key():
    profile, keys = shop()
    keys["tables"]["customers"]["primary_key"] = None            # k = 0 for every candidate
    profile["containment"][0]["containment"] = 0.8
    result = run(profile, keys)
    assert result["adaptive"] and result["weights"]["overlap"] == 55 and result["weights"]["target_key"] == 0
    assert abs(one(result, ("orders", "customer_id"), ("customers", "customer_id"))["score"] - (55 * 0.8 + 20 + 15 + 10)) < 1e-3


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


def test_g9_rejects_a_measure_in_a_corner_of_an_id_range():
    """A 1-5 rating inside order ids 1..1000 is contained by chance; customer ids spanning the range are not."""
    for smax, gated in ((5, True), (900, False)):
        profile, keys = shop()
        profile["tables"]["orders"]["columns"]["customer_id"]["numeric"] = {"min": 1, "max": smax}
        profile["tables"]["customers"]["columns"]["customer_id"]["numeric"] = {"min": 1, "max": 1000}
        r = one(run(profile, keys), ("orders", "customer_id"), ("customers", "customer_id"))
        assert (r["decision"] == "rejected_gate" and r["reason"].startswith("G9")) == gated, smax
    assert one(run(profile, keys, min_range_span=None), ("orders", "customer_id"), ("customers", "customer_id"))["decision"] == "accepted"
