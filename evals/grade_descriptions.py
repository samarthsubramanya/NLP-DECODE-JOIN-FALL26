"""Grade Stage 3 descriptions against KaggleDBQA's human column descriptions (KaggleDBQA_tables.json).

    python evals/grade_descriptions.py --dataset kaggledbqa_obfuscated --run ddb_low

Same measures as grade_bird.py: cosine of "name description" to the human text (qwen3-embedding-8b),
token F1, cosine to the next column's text in the same table (chance), and own text ranked first.
Writes out/descriptions/<dataset>/<run>/grade_descriptions.json. Needs VOYAGER_API_KEY in .env.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "evals")]
import labels                                   # noqa: E402
from grade_joins_all import load_gold           # noqa: E402
from textsim import _mean, cos, embed, token_f1  # noqa: E402

TABLES = ROOT.parent / "datasets/KaggleDBQA/KaggleDBQA_tables.json"


def references():
    """{(db lower, table lower, column lower): human description}."""
    out = {}
    for d in json.loads(TABLES.read_text()):
        for (ti, c), text in zip(d["column_names_original"], d["column_descriptions"]):
            if ti >= 0 and text:
                out[(d["db_id"].lower(), d["table_names_original"][ti].lower(), c.lower())] = text
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    if labels.documented(args.dataset) != "kaggledbqa":
        sys.exit("only KaggleDBQA; use grade_bird.py for BIRD")
    _, mapping, _ = load_gold(args.dataset)
    refs, rows = references(), []
    s3 = ROOT / f"out/descriptions/{args.dataset}/{args.run}"
    for f in sorted(s3.glob("*.json")):
        d = json.loads(f.read_text())
        if "tables" not in d:
            continue
        db = d["db_id"]
        for t, td in d["tables"].items():
            for c, cd in td["columns"].items():
                rt, rc = labels.back(mapping, db, t, c)
                ref = refs.get((db.lower(), rt.lower(), rc.lower()))
                pred = " ".join(x for x in (cd.get("name"), cd.get("description")) if x)
                if ref and pred:
                    rows.append({"db": db, "table": rt, "column": rc, "status": cd["status"], "pred": pred, "ref": ref})
    n = len(rows)
    embs = embed([r["pred"] for r in rows] + [r["ref"] for r in rows])
    by_table = defaultdict(list)
    for i, r in enumerate(rows):
        by_table[(r["db"], r["table"])].append(i)
    for i, r in enumerate(rows):
        r["cosine"] = cos(embs[i], embs[n + i])
        r["token_f1"] = token_f1(r["pred"], r["ref"])
        peers = by_table[(r["db"], r["table"])]
        if len(peers) > 1:
            j = peers[(peers.index(i) + 1) % len(peers)]
            r["cosine_other_column"] = cos(embs[i], embs[n + j])
            r["first"] = all(cos(embs[i], embs[n + k]) <= r["cosine"] for k in peers if k != i)
    report = {"dataset": args.dataset, "run": args.run, "graded_columns": n,
              "status": dict(Counter(r["status"] for r in rows)),
              "mean_cosine": _mean(r["cosine"] for r in rows),
              "mean_cosine_other_column": _mean(r.get("cosine_other_column") for r in rows),
              "own_reference_ranked_first": _mean(r.get("first") for r in rows),
              "mean_token_f1": _mean(r["token_f1"] for r in rows)}
    (s3 / "grade_descriptions.json").write_text(json.dumps({**report, "columns": rows}, indent=1))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
