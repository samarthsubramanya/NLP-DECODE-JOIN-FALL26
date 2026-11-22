"""Does a wider LLM band for sparse sources pay off? (follow-up to the sparsity sweep, item 3)

One perturbation point per dataset is built in memory as in sparsity_sweep.py, scored with the null-rate cap and
the containment adjustment used only at G6 (variant capf_gate without the fallback), then Stage 3's adjudication
runs twice on the same candidates:
    normal   the usual band [accept, accept + delta)
    widened  the band plus rejected records scoring in [accept - delta, accept) whose source is sparse
             (null rate >= SPARSE) or that passed G6 only through the containment adjustment
The prompt shows the band as [accept - delta, accept + delta) for the widened run. Graded like the sweep
(graphs.llm against declared FKs). LLM calls go through describe_columns.Voyager (cached in out/llm_cache).

Usage (from the repo root):
    python evals/llm_band_probe.py --dataset bird_stripped --mode fk_null --rate 0.9
"""
import argparse
import copy
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "evals")]
import describe_columns as dc                   # noqa: E402
import foreign_keys as fk                       # noqa: E402
import labels                                   # noqa: E402
import primary_keys as pk                       # noqa: E402
import sparsity_sweep as sw                     # noqa: E402
from grade_beaver import prf                    # noqa: E402
from grade_joins_all import load_gold           # noqa: E402
from profile_columns import DEFAULTS as P       # noqa: E402

SPARSE = 0.3
OUT = ROOT / "out/eval/sparsity/llm_band"


def adjudicate_retrying(llm, prof, keys, joins, cfg, attempts=4, wait=60):
    """The gateway sometimes answers 500 past Voyager's own retries; finished calls are cached, so a retry resumes."""
    for i in range(attempts):
        try:
            return dc.adjudicate(llm, prof, keys, joins, cfg)   # a failed call leaves every decision unchanged
        except (RuntimeError, OSError, ConnectionError) as e:   # gateway 500s and dropped connections
            if i == attempts - 1:
                raise
            print(f"  retry {i + 1} after: {str(e)[:80]}", flush=True)
            time.sleep(wait)


def grade(joins, gold, mapping, db, graph):
    edges = {labels.norm_edge(mapping, db, e) for e in joins["graphs"][graph]}
    return len(edges & gold), len(edges)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--mode", required=True, choices=["clean", "pk_null", "fk_null", "row_sample"])
    ap.add_argument("--rate", type=float, default=0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--reasoning-effort", default="low")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"{args.dataset}__{args.mode}_{args.rate}"
    llm_cfg = {**dc.DEFAULTS, "reasoning_effort": args.reasoning_effort, "workers": args.workers}
    llm = dc.Voyager(llm_cfg, OUT / f"{tag}.calls.jsonl")
    jcfg = {**fk.DEFAULTS, "keys_run": "cap_gate", "containment_adaptive": 0.5, "adaptive_gate_only": True}

    schemas, mapping, gold_of = load_gold(args.dataset)
    tot = {k: [0, 0] for k in ("stats", "normal", "widened")}
    n_gold = calls = 0
    rows = []
    for d in json.loads((ROOT / f"out/filtered/{args.dataset}/databases.json").read_text()):
        db = d["db_id"]
        clean = json.loads((ROOT / f"out/profiles/{args.dataset}/{db}.json").read_text())
        gold = set(gold_of(db))
        if not gold:
            continue
        names = sw.shown_names(clean, mapping, db)
        oriented = [o for o in (sw.orient([names[x] for x in p], clean) for p in gold if all(x in names for x in p)) if o]
        con = sw.copy_db(d["duckdb_file"], d["duckdb_schema"], d["tables"])
        sw.perturb(con, args.mode, args.rate, sorted({t for _, t in oriented}) if args.mode == "pk_null"
                   else sorted({s for s, _ in oriented}), d["tables"], args.seed)
        prof = sw.profile_database(con, "s", d["tables"], {**P, "seed": args.seed}, db, log=lambda *_: None)
        con.close()
        keys = pk.score_database(prof, {**pk.DEFAULTS, "use_names": not args.dataset.endswith("_obfuscated")})
        base = fk.score_database(prof, keys, jcfg)
        row = {"db": db, "gold": len(gold), "stats": grade(base, gold, mapping, db, "stats")}
        for name in ("normal", "widened"):
            joins = copy.deepcopy(base)
            added = dc.widen_band(joins, prof, SPARSE) if name == "widened" else 0
            band = [r for r in joins["candidates"] if r["decision"] == "band"]
            verdicts = adjudicate_retrying(llm, prof, keys, joins, llm_cfg) if band else {}
            if not band:
                joins["graphs"]["llm"] = joins["graphs"]["direct"]
            tp, pr = grade(joins, gold, mapping, db, "llm")
            judged = [r for r in joins["candidates"] if r["decision"].startswith("llm_")]
            row[name] = {"tp": tp, "predicted": pr, "band": len(band), "added": added, "verdicts": verdicts,
                         "band_gold": sum(labels.norm_edge(mapping, db, r) in gold for r in judged),
                         "added_gold_accepted": sum(bool(r.get("widened")) and r["decision"] == "llm_accept"
                                                    and labels.norm_edge(mapping, db, r) in gold for r in judged),
                         "added_other_accepted": sum(bool(r.get("widened")) and r["decision"] == "llm_accept"
                                                     and labels.norm_edge(mapping, db, r) not in gold for r in judged)}
            tot[name][0] += tp
            tot[name][1] += pr
        tot["stats"][0] += row["stats"][0]
        tot["stats"][1] += row["stats"][1]
        n_gold += len(gold)
        rows.append(row)
        print(f"{db}: normal band {row['normal']['band']} {row['normal']['verdicts']} | widened +{row['widened']['added']} "
              f"{row['widened']['verdicts']}", flush=True)
    summary = {"dataset": args.dataset, "mode": args.mode, "rate": args.rate, "gold": n_gold,
               **{k: prf(v[0], v[1], n_gold) for k, v in tot.items()},
               "added": sum(r["widened"]["added"] for r in rows),
               "added_gold_accepted": sum(r["widened"]["added_gold_accepted"] for r in rows),
               "added_other_accepted": sum(r["widened"]["added_other_accepted"] for r in rows)}
    (OUT / f"{tag}.json").write_text(json.dumps({"summary": summary, "per_db": rows}, indent=1))
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
