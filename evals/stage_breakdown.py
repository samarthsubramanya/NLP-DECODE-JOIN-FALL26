"""Stage 1, 2a and 2b results as CSVs, with every formula term next to the counts it is built from. No LLM.

    python evals/stage_breakdown.py --dataset bird_stripped --run breakdown

Reads  out/profiles/<dataset>/, out/keys/<dataset>/<run>/, out/joins/<dataset>/<run>/
Writes out/breakdown/<dataset>/
    stage1_columns.csv   one row per column: null_rate = nulls / rows, uniqueness = distinct / non_null,
                         density = distinct / (max - min + 1), key_like
    stage1_pairs.csv     one row per stored containment pair: containment = intersection / source_distinct,
                         coverage = intersection / target_distinct, dist = 1 - emd / emd_max over L buckets
    stage2a_columns.csv  one row per column: u = distinct / rows, f(u), n, d, p with their weights,
                         base = sum of weight x term, multipliers, boost, phi, score = (base x mults + boost) x phi
    stage2b_pairs.csv    one row per candidate: each term, its inputs, weight and points, raw = sum of points,
                         penalties, fan_out, cap, score, decision
gold_pk and gold_join are evaluation labels (never read by the stages); blank where nothing is declared.
"""
import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "evals")]
import labels                                   # noqa: E402
from duckdb_driver import open_db               # noqa: E402
from eval_primary_keys import gold_keys         # noqa: E402
from grade_beaver import ddl_labels             # noqa: E402
from grade_joins_all import load_gold           # noqa: E402
from profile_columns import key_like            # noqa: E402

TERMS_2B = ("overlap", "distribution", "name", "cardinality", "target_key", "nulls")


def r4(x):
    return round(x, 4) if isinstance(x, float) else x


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        if not rows:
            return
        cols = list(dict.fromkeys(k for r in rows for k in r))
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows({k: r4(v) for k, v in r.items()} for r in rows)


def gold_pks(dataset, db, con, schema):
    """{table lower: [pk columns lower]}, or None when the benchmark declares no keys for this database."""
    base = labels.documented(dataset)
    if base in ("bird", "spider"):
        return labels.gold(con, f"{base}_metadata", schema)[0]
    if base == "fiben":
        return {t: [c.lower() for c in cs] for t, cs in gold_keys(con, base, schema).items()}
    if base == "beaver" and db != "dw":
        return ddl_labels(con, db)[0]
    return None


def stage1(db, prof):
    cols, pairs = [], []
    for t, tp in prof["tables"].items():
        for c, p in tp["columns"].items():
            num = p.get("numeric") or {}
            span = num["max"] - num["min"] + 1 if num.get("max") is not None and p["family"] == "int" else None
            cols.append({
                "db": db, "table": t, "column": c, "position": p["position"], "declared_type": p["declared_type"],
                "family": p["family"], "role": p["role"], "pattern": p["pattern"],
                "rows": p["rows"], "nulls": p["nulls"], "non_null": p["rows"] - p["nulls"], "null_rate": p["null_rate"],
                "placeholders": round(p["placeholder_rate"] * p["rows"]), "placeholder_rate": p["placeholder_rate"],
                "zeros": round(p["zero_rate"] * p["rows"]), "zero_rate": p["zero_rate"],
                "distinct": p["distinct"], "uniqueness": p["uniqueness"],
                "min": num.get("min"), "max": num.get("max"), "range_span": span, "density": num.get("density"),
                "key_like": key_like(p), "dense_pick": p.get("dense_pick", False),
                "table_all_columns_null": all(x["nulls"] for x in tp["columns"].values()),
            })
    for x in prof.get("containment", []):
        b = x.get("dist_buckets")
        L = len(b) if b else None
        emd_max = (L - 1) / 2 if L else None
        pairs.append({
            "db": db, "source_table": x["source"][0], "source_column": x["source"][1],
            "target_table": x["target"][0], "target_column": x["target"][1],
            "intersection": x["intersection"], "source_distinct": x["source_distinct"], "containment": x["containment"],
            "target_distinct": x["target_distinct"], "coverage": x["coverage"], "source_unique": x["source_unique"],
            "buckets_L": L, "bucket_hits": " ".join(map(str, b)) if b else None,
            "emd": None if x["dist"] is None else (1 - x["dist"]) * emd_max, "emd_max": emd_max, "dist": x["dist"],
        })
    return cols, pairs


def stage2a(db, prof, keys, gold, mapping):
    rows = []
    for t, kt in keys["tables"].items():
        gold_t = None if gold is None else gold.get(t.lower(), [])
        for c, s in kt["columns"].items():
            p = prof["tables"][t]["columns"][c]
            real = labels.back(mapping, db, t, c)[1].lower()
            row = {"db": db, "table": t, "column": c, "rows": p["rows"], "distinct": p["distinct"], "nulls": p["nulls"],
                   "position": p["position"], "rejected": s.get("rejected")}
            if "score" in s:
                pa, w, m, bp = s["parts"], s["weights"], s["multipliers"], s.get("boost_parts", {})
                row.update({
                    "u": pa["u"], "f_u": pa["f_u"], "w_u": w["u"], "pts_u": w["u"] * pa["f_u"],
                    "name_n": pa.get("name"), "w_name": w.get("name", 0),
                    "pts_name": w.get("name", 0) * (pa.get("name") or 0),
                    "dtype": p["declared_type"], "dtype_d": pa["dtype"], "w_dtype": w["dtype"],
                    "pts_dtype": w["dtype"] * pa["dtype"],
                    "pattern_kind": pa["pattern_kind"], "pattern_p": pa["pattern"], "w_pattern": w["pattern"],
                    "pts_pattern": w["pattern"] * pa["pattern"], "base": s["base"],
                    "mult_nulls": m.get("nulls"), "mult_atypical_name": m.get("atypical_name"),
                    "mult_fk_likelihood": m.get("fk_likelihood"),
                    "boost_surrogate": bp.get("surrogate"), "boost_referenced": bp.get("referenced"),
                    "referenced_by": " ".join(pa.get("referenced_by", [])), "boost": s["boost"],
                    "phi": s["phi"], "score": s["score"], "accepted": s["score"] >= keys["accept"],
                })
            row["chosen_pk"] = kt["primary_key"] == c
            row["evidence"] = kt["evidence"] if row["chosen_pk"] else None
            row["gold_pk"] = None if gold_t is None else real in gold_t
            rows.append(row)
    return rows


def stage2b(db, prof, joins, gold_fks, mapping):
    rows = []
    tables = prof["tables"]
    for r in joins["candidates"]:
        st, sc, tt, tc = r["source"]["table"], r["source"]["column"], r["target"]["table"], r["target"]["column"]
        a = tables[st]["columns"][sc]
        row = {"db": db, "source_table": st, "source_column": sc, "target_table": tt, "target_column": tc,
               "decision": r["decision"], "reason": r.get("reason"), "partial_overlap": r.get("partial_overlap"),
               "containment": r["containment"], "coverage": r["coverage"], "cardinality": r["cardinality"]}
        if "weights_used" in r:
            pa, w = r["parts"], r["weights_used"]
            inputs = {
                "overlap": {},
                "distribution": {"dist_bucket_hits": " ".join(map(str, r["dist_buckets"])) if r["dist_buckets"] else None},
                "name": {"name_matched": r.get("name_via")},
                "cardinality": {"source_table_rows": tables[st]["rows"], "source_distinct": a["distinct"],
                                "rho": tables[st]["rows"] / a["distinct"]},
                "target_key": {"target_pk_score": r["target_pk_score"]},
                "nulls": {"source_nulls": a["nulls"], "source_rows": a["rows"], "source_null_rate": a["null_rate"]},
            }
            raw = 0.0
            for k in TERMS_2B:
                v = pa.get(k)
                pts = (w.get(k, 0) * v) if v is not None else 0.0
                raw += pts
                row.update({**inputs[k], k: v, f"w_{k}": w.get(k, 0), f"pts_{k}": pts})
            row.update({"raw": raw, "pen_orphan": r["penalties"].get("orphan"),
                        "pen_incompatible": r["penalties"].get("incompatible_types"),
                        "score_before_fan_out": r["score_before_fan_out"], "fan_out": r.get("fan_out"),
                        "dist_capped": r.get("dist_capped"), "score": r.get("score")})
        elif "score" in r:
            row["score"] = r["score"]
        row["gold_join"] = None if gold_fks is None else labels.norm_edge(mapping, db, r) in gold_fks
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--run", required=True, help="keys and joins run name")
    args = ap.parse_args()
    ds = args.dataset
    _, mapping, gold = load_gold(ds)
    dbs = json.loads((ROOT / f"out/filtered/{labels.documented(ds)}/databases.json").read_text())
    out = {k: [] for k in ("stage1_columns", "stage1_pairs", "stage2a_columns", "stage2b_pairs")}
    for d in dbs:
        db = d["db_id"]
        prof = json.loads((ROOT / f"out/profiles/{ds}/{db}.json").read_text())
        keys = json.loads((ROOT / f"out/keys/{ds}/{args.run}/{db}.json").read_text())
        joins = json.loads((ROOT / f"out/joins/{ds}/{args.run}/{db}.json").read_text())
        c, p = stage1(db, prof)
        out["stage1_columns"] += c
        out["stage1_pairs"] += p
        con = open_db(d["duckdb_file"])
        out["stage2a_columns"] += stage2a(db, prof, keys, gold_pks(ds, db, con, d["duckdb_schema"]), mapping)
        fks = gold(db)
        out["stage2b_pairs"] += stage2b(db, prof, joins, fks if fks or labels.documented(ds) != "beaver" else None,
                                        mapping)
    for name, rows in out.items():
        write(ROOT / f"out/breakdown/{ds}/{name}.csv", rows)
    print(json.dumps({"dataset": ds, **{k: len(v) for k, v in out.items()}}))


if __name__ == "__main__":
    main()
