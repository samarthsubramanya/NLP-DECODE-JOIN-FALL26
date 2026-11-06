"""Where joins go wrong: one report per dataset from a finished Stage 3 run. No LLM.

    missed      why each gold FK is absent from graphs.llm: an empty table, no key-like end, too little overlap,
                or the step that dropped the candidate
    decisions   how candidates ended up, split into gold and not gold
    false joins how many non-gold edges were auto-accepted and how many the LLM accepted
    thin        final edges with little value evidence (source under 5 distinct values, or no distribution)
    questions   for retrieval graphs: how many questions need a table with no join in the final graph

    python evals/join_attribution.py --dataset beaver --run ddb_low
Prints one JSON line. Gold comes from grade_joins_all.load_gold.
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "evals")]
import labels                                   # noqa: E402
from grade_joins_all import load_gold           # noqa: E402
from profile_columns import key_like            # noqa: E402

RANK = ["accepted", "llm_accept", "llm_abstain", "llm_reject", "band", "rejected_score", "rejected_gate"]


def outcome(r):
    return "gate:" + re.split(r"[: ]", r.get("reason", ""))[0] if r["decision"] == "rejected_gate" else r["decision"]


def why_missed(fk, best, columns):
    if fk in best:
        return "dropped at " + outcome(best[fk])
    ends = [columns.get(x) for x in fk]
    if None in ends:
        return "no pair: column not profiled"
    if any(rows == 0 for _, rows in ends):
        return "no pair: empty table"
    if not any(key_like(p) for p, _ in ends):
        return "no pair: no key-like end (" + ("nulls" if any(p["nulls"] for p, _ in ends) else "duplicates") + ")"
    return "no pair: containment under .5 or different type"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--run", required=True, help="out/descriptions/<dataset>/<run>")
    args = ap.parse_args()
    schemas, mapping, gold = load_gold(args.dataset)
    decisions, missed, false_joins, thin = {True: Counter(), False: Counter()}, Counter(), Counter(), Counter()
    isolated = {}
    for db in schemas:
        joins = json.loads((ROOT / f"out/descriptions/{args.dataset}/{args.run}/joins/{db}.json").read_text())
        prof = json.loads((ROOT / f"out/profiles/{args.dataset}/{db}.json").read_text())
        fks = gold(db)
        columns = {}
        for t, tp in prof["tables"].items():
            for c, p in tp["columns"].items():
                rt, rc = labels.back(mapping, db, t, c)
                columns[(rt.lower(), rc.lower())] = (p, tp["rows"])
        pairs = {(tuple(x["source"]), tuple(x["target"])): x for x in prof.get("containment", [])}
        best = {}
        for r in joins["candidates"]:
            k = labels.norm_edge(mapping, db, r)
            if k not in best or RANK.index(r["decision"]) < RANK.index(best[k]["decision"]):
                best[k] = r
        for k, r in best.items():
            decisions[k in fks][outcome(r)] += 1
        final = {labels.norm_edge(mapping, db, e) for e in joins["graphs"]["llm"]}
        for fk in fks:
            missed["found" if fk in final else why_missed(fk, best, columns)] += 1
        for e in joins["graphs"]["llm"]:
            k = labels.norm_edge(mapping, db, e)
            if k not in fks:
                false_joins[e["decision"]] += 1
            pair = pairs.get(((e["source"]["table"], e["source"]["column"]), (e["target"]["table"], e["target"]["column"])))
            if pair and (pair["source_distinct"] < 5 or pair["dist"] is None):
                thin["gold" if k in fks else "not gold"] += 1
        linked = {t for e in joins["graphs"]["llm"] for t in (e["source"]["table"], e["target"]["table"])}
        isolated[db] = sorted(t for t, tp in prof["tables"].items() if tp["rows"] and t not in linked)
    print(json.dumps({"dataset": args.dataset, "run": args.run, "missed": dict(missed),
                      "decisions": {"gold": dict(decisions[True]), "not_gold": dict(decisions[False])},
                      "false_joins": dict(false_joins), "thin": dict(thin), "isolated_tables": isolated}))


if __name__ == "__main__":
    main()
