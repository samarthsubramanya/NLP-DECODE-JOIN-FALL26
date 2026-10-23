"""
Grade Stage 2a primary keys (src/primary_keys.py) against the declared keys.

The declared keys are evaluation labels only; this is the one script that reads them:
    bird, spider   <dataset>_metadata.table_info: SQLite's table_xinfo, `pk` = position in the key
    fiben          duckdb_constraints() of the FIBEN schema
    beaver, kaggledbqa declare no primary keys, so there is nothing to grade

Each table falls in one bucket by its gold key:
    single     one-column key: correct / wrong column / missed (no prediction)
    composite  out of scope: abstained (good) / predicted a component / predicted another column
    none       no declared key: abstained (good) / predicted something

    precision = correct / tables with a prediction
    recall    = correct / tables with a single-column gold key
    candidate recall = gold key among all accepted candidates, not only the top one

Usage:
    python evals/eval_primary_keys.py --dataset bird
    python evals/eval_primary_keys.py --dataset spider --no-names --run no_names

Output: out/keys/<dataset>/<run>/  <db_id>.json (predictions), eval.json, errors.jsonl
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))   # pipeline modules live in src/

from duckdb_driver import open_db
from primary_keys import ROOT, add_args, config_from, load_profile, score_database, work_list


def gold_keys(con, dataset, schema):
    """{table_lower: [pk columns in key order]} for one DuckDB schema; {} when nothing is declared."""
    meta = f"{dataset}_metadata"
    has_meta = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = 'table_info'", [meta]
    ).fetchone()[0]
    out = {}
    if has_meta:
        for table, meta_json in con.execute(
            f'SELECT t.table_name, t.metadata_json FROM "{meta}".table_info t '
            f'JOIN "{meta}".files f USING (source_path) WHERE f.duckdb_schema = ?', [schema]
        ).fetchall():
            cols = sorted((c for c in json.loads(meta_json)["table_xinfo"] if c["pk"]), key=lambda c: c["pk"])
            out[table.lower()] = [c["name"] for c in cols]
        return out
    for table, cols in con.execute(
        "SELECT table_name, constraint_column_names FROM duckdb_constraints() "
        "WHERE schema_name = ? AND constraint_type = 'PRIMARY KEY'", [schema]
    ).fetchall():
        out[table.lower()] = list(cols)
    return out


def why_not(scored):
    """Short reason a gold column was not the prediction."""
    if "rejected" in scored:
        return scored["rejected"]
    return (f"score {scored['score']} (base {scored['base']}, x{scored['multipliers'] or 1}, "
            f"+{scored['boost']}, phi {scored['phi']}, pattern {scored['parts']['pattern_kind']})")


def reason_kind(error, accept):
    """'blacklist: financial (amount)' -> 'blacklist'; scores -> below cutoff / outscored."""
    r = error["gold_reason"]
    if r.startswith("score "):
        return "scored below accept" if float(r.split()[1]) < accept else "outscored by another column"
    return r.split(":")[0]


def grade(dataset, db, keys, gold):
    counts, errors = Counter(), []
    for table, t in keys["tables"].items():
        g = gold.get(table.lower(), [])   # a table without a declared key has none
        pred = t["primary_key"]
        cands = {c["column"].lower() for c in t["candidate_keys"]}
        if t.get("evidence") == "name_only":
            # R5 guesses on empty tables are graded on their own and left out of the main metrics
            counts["name_only_predicted"] += 1
            counts["name_only_correct"] += len(g) == 1 and pred.lower() == g[0].lower()
            pred = None
        elif t.get("evidence") == "only_candidate":
            counts["only_candidate_predicted"] += 1
            counts["only_candidate_correct"] += len(g) == 1 and pred.lower() == g[0].lower()
        if pred:
            counts["predicted"] += 1
        g_low = [c.lower() for c in g]
        if len(g) == 1:
            counts["single"] += 1
            empty = all(s.get("rejected") == "empty table" for s in t["columns"].values())
            counts["single_nonempty"] += not empty
            if pred and pred.lower() == g_low[0]:
                counts["correct"] += 1
                continue
            counts["candidate_hit"] += g_low[0] in cands
            outcome = "wrong" if pred else "missed"
            counts[outcome] += 1
            gold_col = next((c for c in t["columns"] if c.lower() == g_low[0]), None)
            errors.append({
                "db_id": db["db_id"], "table": table, "outcome": outcome, "gold": g[0], "predicted": pred,
                "predicted_score": t["columns"][pred]["score"] if pred else None,
                "gold_reason": why_not(t["columns"][gold_col]) if gold_col else "gold column not in table",
            })
        elif len(g) > 1:
            counts["composite"] += 1
            key = "composite_abstained" if not pred else (
                "composite_component" if pred.lower() in g_low else "composite_other")
            counts[key] += 1
            if pred:
                errors.append({"db_id": db["db_id"], "table": table, "outcome": key, "gold": g, "predicted": pred,
                               "predicted_score": t["columns"][pred]["score"]})
        else:
            counts["no_key"] += 1
            counts["no_key_abstained" if not pred else "no_key_predicted"] += 1
            if pred:
                errors.append({"db_id": db["db_id"], "table": table, "outcome": "no_key_predicted",
                               "gold": [], "predicted": pred, "predicted_score": t["columns"][pred]["score"]})
    return counts, errors


def summarize(c):
    p = c["correct"] / c["predicted"] if c["predicted"] else 0.0
    r = c["correct"] / c["single"] if c["single"] else 0.0
    return {
        "tables": c["single"] + c["composite"] + c["no_key"],
        "single_key_tables": c["single"], "composite_key_tables": c["composite"], "no_key_tables": c["no_key"],
        "predicted": c["predicted"], "correct": c["correct"], "wrong": c["wrong"], "missed": c["missed"],
        "precision": round(p, 4), "recall": round(r, 4), "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0,
        "candidate_recall": round((c["correct"] + c["candidate_hit"]) / c["single"], 4) if c["single"] else 0.0,
        # empty tables give the data nothing to go on; recall over the ones with rows
        "single_key_tables_with_rows": c["single_nonempty"],
        "recall_tables_with_rows": round(c["correct"] / c["single_nonempty"], 4) if c["single_nonempty"] else 0.0,
        "only_candidate": {"predicted": c["only_candidate_predicted"], "correct": c["only_candidate_correct"]},
        "name_only": {"predicted": c["name_only_predicted"], "correct": c["name_only_correct"]},
        "composite": {k: c[f"composite_{k}"] for k in ("abstained", "component", "other")},
        "no_key": {k: c[f"no_key_{k}"] for k in ("abstained", "predicted")},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_args(ap)
    args = ap.parse_args()
    cfg = config_from(args)
    out_dir = ROOT / "out" / "keys" / args.dataset / args.run
    out_dir.mkdir(parents=True, exist_ok=True)

    total, per_db, all_errors = Counter(), {}, []
    for db in work_list(args):
        keys = score_database(load_profile(args.dataset, db, args.reprofile), cfg)
        (out_dir / f"{db['db_id']}.json").write_text(json.dumps(keys, indent=1, sort_keys=True))
        gold = gold_keys(open_db(db["duckdb_file"]), args.dataset, db["duckdb_schema"])
        if not gold:
            print(f"  {db['db_id']}: no declared primary keys, not graded")
            continue
        counts, errors = grade(args.dataset, db, keys, gold)
        total.update(counts)
        all_errors += errors
        per_db[db["db_id"]] = summarize(counts)
        s = per_db[db["db_id"]]
        print(f"  {db['db_id']:<32} single {s['single_key_tables']:>3}  correct {s['correct']:>3}  "
              f"wrong {s['wrong']:>3}  missed {s['missed']:>3}  P {s['precision']:.2f}  R {s['recall']:.2f}")

    if not per_db:
        print(f"{args.dataset}: no gold primary keys found, nothing to grade")
        return
    overall = summarize(total)
    reasons = Counter(reason_kind(e, cfg["accept"]) for e in all_errors if e["outcome"] in ("wrong", "missed"))
    report = {"dataset": args.dataset, "run": args.run, "config": cfg, "overall": overall,
              "gold_pk_lost_because": dict(reasons.most_common()), "per_db": per_db}
    (out_dir / "eval.json").write_text(json.dumps(report, indent=1))
    with open(out_dir / "errors.jsonl", "w") as f:
        for e in all_errors:
            f.write(json.dumps(e) + "\n")

    o = overall
    print(f"\n{args.dataset} [{args.run}]  tables {o['tables']}  (single {o['single_key_tables']}, "
          f"composite {o['composite_key_tables']}, none {o['no_key_tables']})")
    print(f"  precision {o['precision']:.3f}  recall {o['recall']:.3f}  F1 {o['f1']:.3f}  "
          f"candidate recall {o['candidate_recall']:.3f}")
    print(f"  recall on tables with rows {o['recall_tables_with_rows']:.3f} "
          f"({o['single_key_tables_with_rows']}/{o['single_key_tables']} single-key tables have rows)")
    print(f"  composite tables: {o['composite']}   no-key tables: {o['no_key']}")
    if o["only_candidate"]["predicted"]:
        print(f"  R2 only-candidate picks (in the metrics above): {o['only_candidate']}")
    if o["name_only"]["predicted"]:
        print(f"  R5 name-only picks on empty tables (NOT in the metrics above): {o['name_only']}")
    print(f"  why gold keys were lost: {dict(reasons.most_common())}")
    print(f"  -> {out_dir.relative_to(ROOT)}/eval.json, errors.jsonl")


if __name__ == "__main__":
    main()
