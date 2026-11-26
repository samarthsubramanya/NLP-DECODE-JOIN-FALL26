"""Distance from thresholds: how far each candidate join sits from the cutoffs that decided it.

For every Stage 2b candidate (and Stage 3's verdict when there is one) this gives the signed margin to
    accept           score - accept               (60: below = rejected_score)
    auto             score - (accept + delta)     (70: at or above = auto-accepted, below = LLM band)
    tau              containment - containment_min (G6, 0.75)
    dist             dist - dist_no_accept_below  (QuantileMD cap, 0.3)
    target_pk        Stage 2a score of the target - 70 (None when 2a rejected the target outright)
and whether it is gold. A false join that sits 2 points above the auto line is a threshold problem: a small shift
removes it. One 30 points above it needs new evidence, no threshold will help. The summary splits every error
into near (within --near points / 0.05) and far, per graph:
    direct  heuristics only (auto-accepted)        cutoff = auto line
    stats   no LLM (band accepted)                 cutoff = accept line
    llm     after Stage 3 (when the run has it)    cutoff = auto line for direct edges, the LLM for band edges

Works on any finished run, no new pipeline run needed:
    python evals/sparsity/margins.py --dataset bird_stripped --run ddb                # Stage 2b run (out/joins)
    python evals/sparsity/margins.py --dataset bird_stripped --run ddb_low --stage3   # Stage 3 run (out/descriptions)
Writes out/sparsity/margins/<dataset>__<run>.jsonl (one row per candidate) and prints the summary as one JSON line.
"""
import argparse
import json
from collections import Counter
from pathlib import Path

import common
from common import Gold, OUT, append_jsonl

NEAR_POINTS, NEAR_SHARE = 5.0, 0.05
PK_ACCEPT = 70


def threshold_values(cfg):
    return {"accept": cfg["accept"], "auto": cfg["accept"] + cfg["delta"], "tau": cfg["containment_min"],
            "dist": cfg.get("dist_no_accept_below")}


def margins(rec, cfg):
    """Signed margins of one candidate record; None where the record has no such number."""
    th = threshold_values(cfg)
    s, d = rec.get("score"), rec.get("dist")
    pk = rec.get("target_pk_score")
    return {
        "accept": None if s is None else round(s - th["accept"], 3),
        "auto": None if s is None else round(s - th["auto"], 3),
        "tau": None if rec.get("containment") is None else round(rec["containment"] - th["tau"], 4),
        "dist": None if d is None or th["dist"] is None else round(d - th["dist"], 4),
        "target_pk": None if pk is None else round(pk - PK_ACCEPT, 3),
    }


def gate_kind(reason):
    if not reason:
        return None
    return reason.split(":")[0].strip()


def rows_for(db, joins, gold_pairs, norm, cfg, extra=None):
    """One row per candidate with its margins, truth and which graphs it ended up in."""
    in_graph = {g: {norm(e) for e in edges} for g, edges in joins["graphs"].items()}
    out = []
    for r in joins["candidates"]:
        pair = norm(r)
        llm = r.get("llm") or {}
        out.append({
            **(extra or {}), "db": db,
            "source": [r["source"]["table"], r["source"]["column"]], "target": [r["target"]["table"], r["target"]["column"]],
            "gold": pair in gold_pairs, "decision": r["decision"], "gate": gate_kind(r.get("reason")),
            "score": r.get("score"), "containment": r.get("containment"), "dist": r.get("dist"),
            "dist_capped": bool(r.get("dist_capped")), "target_is_pk": r.get("target_is_pk"),
            "llm": llm.get("decision"), "margins": margins(r, cfg),
            "in": sorted(g for g, s in in_graph.items() if pair in s),
        })
    return out


def _near(m, key):
    v = m.get(key)
    if v is None:
        return False
    return abs(v) < (NEAR_SHARE if key in ("tau", "dist") else NEAR_POINTS)


def summarize(rows, graphs=("direct", "stats", "llm")):
    """Errors per graph, split by how close they sit to the cutoff that let them in or kept them out."""
    out = {}
    # a column pair can appear as two records (both directions); count each pair once, by its best record
    best = {}
    for r in rows:
        k = (r["db"], frozenset((tuple(r["source"]), tuple(r["target"]))))
        if k not in best or (r["score"] or -1) > (best[k]["score"] or -1):
            best[k] = r
    uniq = list(best.values())
    for g in graphs:
        if not any(g in r["in"] for r in rows) and g == "llm":
            continue
        line = "accept" if g == "stats" else "auto"
        c = Counter()
        for r in uniq:
            inside, m = g in r["in"], r["margins"]
            if inside and not r["gold"]:
                c["fp"] += 1
                if g == "llm" and r["decision"] == "llm_accept":
                    c["fp_llm_accepted"] += 1
                elif _near(m, line):
                    c["fp_near"] += 1
                else:
                    c["fp_far"] += 1
            elif not inside and r["gold"]:
                c["fn"] += 1
                if r["gate"]:
                    c[f"fn_gate:{r['gate']}"] += 1
                    if r["gate"] == "G6" and _near(m, "tau"):
                        c["fn_near_tau"] += 1
                elif r["score"] is not None and _near(m, line):
                    c["fn_near"] += 1
                else:
                    c["fn_far"] += 1
            elif inside:
                c["tp"] += 1
                if _near(m, line):
                    c["tp_near"] += 1
        out[g] = dict(sorted(c.items()))
    hist = Counter()
    for r in uniq:
        if r["score"] is not None:
            b = int(min(max(r["score"], 0), 99.999) // 5 * 5)
            hist[f"{'gold' if r['gold'] else 'false'}:{b:02d}"] += 1
    out["score_hist"] = dict(sorted(hist.items()))
    gold_targets = Counter()
    for r in uniq:
        if r["gold"] and r["gate"] == "G1":
            gold_targets["gold_target_rejected_by_2a"] += 1
    out.update(gold_targets)
    return out


def load_run(dataset, run, stage3):
    base = common.ROOT / "out" / ("descriptions" if stage3 else "joins") / dataset / run
    jdir = base / "joins" if stage3 else base
    if not jdir.is_dir():
        raise SystemExit(f"{jdir.relative_to(common.ROOT)} is missing")
    return {f.stem: json.loads(f.read_text()) for f in sorted(jdir.glob("*.json"))}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--stage3", action="store_true", help="read out/descriptions/<dataset>/<run>/joins (has graphs.llm)")
    args = ap.parse_args()
    gold = Gold(args.dataset)
    rows = []
    for db, joins in load_run(args.dataset, args.run, args.stage3).items():
        prof_path = common.ROOT / "out" / "profiles" / args.dataset / f"{db}.json"
        prof = json.loads(prof_path.read_text()) if prof_path.exists() else None
        rows += rows_for(db, joins, gold.pairs(db, prof), lambda e, db=db: gold.norm(db, e), joins["config"],
                         {"dataset": args.dataset, "run": args.run})
    path = OUT / "margins" / f"{args.dataset}__{args.run}.jsonl"
    if path.exists():
        path.unlink()
    append_jsonl(path, rows)
    print(json.dumps({"dataset": args.dataset, "run": args.run, "rows": str(Path(path).relative_to(common.ROOT)),
                      **summarize(rows)}))


if __name__ == "__main__":
    main()
