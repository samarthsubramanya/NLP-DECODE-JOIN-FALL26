"""Combine graded joins and retrieval scores into the ablation table, with the change from FULL (A0).

From new ablation runs (grade.py output + score_joingr.py "pipeline" lines saved to a file):

    python evals/ablation/table.py --joins out/ablation/fiben_stripped/joins.jsonl \
        --retrieval out/ablation/fiben_stripped/retrieval.jsonl \
        --export A0_full=ddb_full A1_noqmd=abl_noqmd_full A2_nollm=abl_nollm_full \
                 A3_nodesc=ddb_joins_only A4_heuristics=abl_heur_full

From the results already committed in out/eval/ddb/ (no runs needed; FULL, -LLM joins, -descriptions retrieval):

    python evals/ablation/table.py --committed fiben_stripped

Prints a markdown table. Retrieval = bge encoder; walk = JoinGR heuristic walk, merged = walk + 2 dense slots.
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENC = "bge"
COLS = ["Config", "Edge P", "Edge R", "Edge F1", "Edges", "False pos.", "Δ F1", "Δ FP",
        "Walk PR@15", "Δ Walk", "Merged PR@15", "Col R@15"]


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def fp(j):
    return j.get("false_positives", j["predicted"] - j["tp"])


def score(r, method, key="PR@15"):
    s = (r or {}).get("scores", {}).get(f"{ENC}/{method}")
    return None if s is None or s.get(key) is None else s[key]


def fmt(v, signed=False, digits=3):
    if v is None:
        return "–"
    if isinstance(v, int) and not signed:
        return str(v)
    return f"{v:+.{digits}f}" if signed else f"{v:.{digits}f}"


def render(rows):
    """rows: [{name, joins: {tp, predicted, precision, recall, f1, ...} | None, retrieval: score_joingr line | None}]"""
    base = rows[0]
    bj, br = base["joins"], base["retrieval"]
    out = ["| " + " | ".join(COLS) + " |", "|" + "---|" * len(COLS)]
    for r in rows:
        j, ret = r["joins"], r["retrieval"]
        walk = score(ret, "heuristic")
        cells = [r["name"],
                 fmt(j and j["precision"]), fmt(j and j["recall"]), fmt(j and j["f1"]),
                 fmt(j and j["predicted"]), fmt(j and fp(j)),
                 fmt(j["f1"] - bj["f1"], True) if j and bj and r is not base else "",
                 (f"{fp(j) - fp(bj):+d}" if j and bj and r is not base else ""),
                 fmt(walk), fmt(walk - score(br, "heuristic"), True) if walk is not None and score(br, "heuristic") is not None and r is not base else "",
                 fmt(score(ret, "merged")), fmt(score(ret, "heuristic", "colR@15"))]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def committed(dataset):
    joins = {j["dataset"]: j for j in read_jsonl(ROOT / "out/eval/ddb/joins.jsonl") if j["run"] == "ddb_low"}
    ret = {(r.get("dataset"), r.get("run")): r for r in read_jsonl(ROOT / "out/eval/ddb/retrieval.jsonl")}
    j = joins.get(dataset)
    if j is None:
        raise SystemExit(f"{dataset} is not in out/eval/ddb/joins.jsonl")
    return [
        {"name": "A0 FULL", "joins": j["llm"], "retrieval": ret.get((dataset, "ddb_full"))},
        {"name": "A2 − LLM verification", "joins": j["stats"], "retrieval": None},
        {"name": "A3 − Column descriptions", "joins": j["llm"], "retrieval": ret.get((dataset, "ddb_joins_only"))},
    ]


def from_runs(joins_path, retrieval_path, export):
    joins = {j["name"]: j for j in read_jsonl(joins_path)}       # later lines win: reruns replace earlier ones
    ret = {}
    if retrieval_path:
        for r in read_jsonl(retrieval_path):
            ret[r.get("run")] = r
    names = list(export) or list(joins)
    return [{"name": n, "joins": joins.get(n), "retrieval": ret.get(export.get(n))} for n in names]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--committed", metavar="DATASET", help="build from out/eval/ddb/ only")
    ap.add_argument("--joins", help="grade.py joins.jsonl")
    ap.add_argument("--retrieval", help="score_joingr.py pipeline lines, one per export run")
    ap.add_argument("--export", nargs="*", default=[], help="NAME=EXPORT_RUN; first one is the FULL baseline")
    args = ap.parse_args()
    if args.committed:
        rows = committed(args.committed)
    elif args.joins:
        rows = from_runs(args.joins, args.retrieval, dict(x.split("=", 1) for x in args.export))
    else:
        ap.error("give --committed DATASET or --joins FILE")
    print(render(rows))


if __name__ == "__main__":
    main()
