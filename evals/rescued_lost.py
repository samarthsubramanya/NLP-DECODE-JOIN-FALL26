"""Questions the pipeline gains and loses against dense alone, from the rankings evals/score_joingr.py saved.

A question counts as found when all its gold tables are in the top 15.
    python evals/rescued_lost.py bird obfuscated ddb_full [--method merged]
Prints one JSON line.
"""
import argparse
import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parent.parent / "results"


def found(path):
    return {q["i"]: set(q["gold"]) <= set(q["ranked"][:15]) for q in json.loads(path.read_text())["per_query"]}


def compare(dense, other):
    missed = [i for i, hit in dense.items() if not hit]
    got = [i for i, hit in dense.items() if hit]
    return {"questions": len(dense), "dense_missed": len(missed), "rescued": sum(other[i] for i in missed),
            "dense_found": len(got), "lost": sum(not other[i] for i in got), "PR@15": sum(other.values()) / len(other)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("benchmark")
    ap.add_argument("variant", choices=["stripped", "obfuscated"])
    ap.add_argument("run", help="out/joingr run, e.g. ddb_full")
    ap.add_argument("--method", default="heuristic", choices=["heuristic", "merged"])
    ap.add_argument("--encoder", default="bge")
    args = ap.parse_args()
    base = RESULTS / args.benchmark
    dense = found(base / f"ddb_alone__{args.variant}/{args.encoder}/vanilla.json")
    other = found(base / f"ddb__{args.benchmark}_{args.variant}__{args.run}/{args.encoder}/{args.method}.json")
    print(json.dumps({"benchmark": args.benchmark, "variant": args.variant, "method": args.method, **compare(dense, other)}))


if __name__ == "__main__":
    assert compare({1: True, 2: False}, {1: False, 2: True})["rescued"] == 1
    main()
