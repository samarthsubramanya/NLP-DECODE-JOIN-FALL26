"""Grade ablation configs: join precision / recall / F1, predicted edges and false positives, plus one row per
candidate edge so any false join can be traced. Gold comes from evals/grade_joins_all.load_gold, unchanged.

Each config is NAME=DATASET:RUN:GRAPH, where RUN is a folder with joins/ (a Stage 3 run under
out/descriptions/<dataset>/) or a bare Stage 2b run under out/joins/<dataset>/, and GRAPH is
llm (final), stats (no LLM: band accepted) or direct (statistics only: accepted, no band).

    python evals/ablation/grade.py --out out/ablation/fiben_stripped \
        A0_full=fiben_stripped:ddb_low:llm \
        A1_noqmd=fiben_stripped:abl_noqmd_low:llm \
        A2_nollm=fiben_stripped:ddb_low:stats \
        A4_heuristics=fiben_stripped:ddb_low:direct

Writes <out>/joins.jsonl (one line per config) and <out>/edges_<NAME>.jsonl (one line per candidate).
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "evals")]


def prf(tp, predicted, gold):
    p = tp / predicted if predicted else 0.0
    r = tp / gold if gold else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"tp": tp, "predicted": predicted, "gold": gold, "false_positives": predicted - tp,
            "precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4)}


def parse_config(text):
    name, _, spec = text.partition("=")
    dataset, run, graph = spec.split(":")
    if graph not in ("llm", "stats", "direct"):
        raise ValueError(f"{text}: graph must be llm, stats or direct")
    return {"name": name, "dataset": dataset, "run": run, "graph": graph}


def joins_dir(dataset, run):
    s3 = ROOT / "out" / "descriptions" / dataset / run / "joins"
    return s3 if s3.is_dir() else ROOT / "out" / "joins" / dataset / run


def edge_rows(name, dataset, db, joins, graph, chosen, fks, norm):
    """One row per candidate: score parts, decision, LLM verdict, whether it is in the graph, gold, false positive."""
    for r in joins["candidates"]:
        pair = norm(r)
        accepted = pair in chosen
        llm = r.get("llm") or {}
        yield {"experiment_id": name, "dataset": dataset, "db": db, "graph": graph,
               "source_table": r["source"]["table"], "source_column": r["source"]["column"],
               "target_table": r["target"]["table"], "target_column": r["target"]["column"],
               "containment": r.get("containment"), "parts": r.get("parts"), "dist": r.get("dist"),
               "score": r.get("score"), "decision": r["decision"], "reason": r.get("reason"),
               "llm_used": bool(llm), "llm_decision": llm.get("decision"), "llm_reason": llm.get("reason"),
               "edge_accepted": accepted, "gold_edge": pair in fks, "is_false_positive": accepted and pair not in fks}


def grade(cfg, out_dir):
    import labels
    from grade_joins_all import load_gold
    schemas, mapping, gold = load_gold(cfg["dataset"])
    fiben = labels.documented(cfg["dataset"]) == "fiben"
    jdir = joins_dir(cfg["dataset"], cfg["run"])
    total, per_db, decisions = Counter(), {}, Counter()
    with (out_dir / f"edges_{cfg['name']}.jsonl").open("w") as f:
        for db in schemas:
            joins = json.loads((jdir / f"{db}.json").read_text())
            if cfg["graph"] not in joins["graphs"]:
                sys.exit(f"{jdir}/{db}.json has no graphs.{cfg['graph']} (has {sorted(joins['graphs'])})")
            fks = gold(db)
            if fiben:   # as grade_joins_all: only FKs whose two tables have rows
                prof = json.loads((ROOT / f"out/profiles/{cfg['dataset']}/{db}.json").read_text())
                rows = {t.lower(): p["rows"] for t, p in prof["tables"].items()}
                fks = {k for k in fks if all(rows.get(t, 0) for t, _ in k)}
            norm = lambda e, db=db: labels.norm_edge(mapping, db, e)   # noqa: E731
            chosen = {norm(e) for e in joins["graphs"][cfg["graph"]]}
            tp = len(chosen & fks)
            total.update(tp=tp, predicted=len(chosen), gold=len(fks))
            per_db[db] = prf(tp, len(chosen), len(fks))
            for row in edge_rows(cfg["name"], cfg["dataset"], db, joins, cfg["graph"], chosen, fks, norm):
                decisions[row["decision"]] += 1
                f.write(json.dumps(row, default=str) + "\n")
    return {**cfg, **prf(total["tp"], total["predicted"], total["gold"]), "decisions": dict(decisions), "per_db": per_db}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="output folder, e.g. out/ablation/fiben_stripped")
    ap.add_argument("configs", nargs="+", help="NAME=DATASET:RUN:GRAPH")
    args = ap.parse_args()
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "joins.jsonl").open("a") as f:
        for text in args.configs:
            res = grade(parse_config(text), out_dir)
            f.write(json.dumps(res) + "\n")
            print(f"{res['name']:16s} P {res['precision']:.3f}  R {res['recall']:.3f}  F1 {res['f1']:.3f}  "
                  f"edges {res['predicted']:4d}  FP {res['false_positives']:4d}")


if __name__ == "__main__":
    main()
