"""Tables from out/sparsity/<dataset>/points.jsonl (sweep.py). Markdown to stdout, a flat CSV next to it.

    python evals/sparsity/report.py --datasets bird_stripped fiben_stripped > docs/sparsity_hp_results.md

Sections, per dataset (means over seeds):
  1. Degradation: FULL's join P / R / F1 and false joins against the sparsity rate, per mode, both graphs
  2. Distance from thresholds: FULL's errors near a cutoff (fixable by moving it) versus far from it
  3. Sparsity-dependent thresholds: adapt_* minus FULL at every point (clean must be 0)
  4. Ablations: every arm minus FULL, on clean data and averaged over the sparse points
  5. Signals: what the adaptive rules measured, against the injected rate
"""
import argparse
import csv
from collections import defaultdict
from statistics import mean

from common import OUT, read_jsonl

GRAPHS = ("direct", "stats")


def avg(xs):
    xs = [x for x in xs if x is not None]
    return mean(xs) if xs else None


def f(v, d=3, signed=False):
    if v is None:
        return "–"
    if isinstance(v, float) or d == 3:
        return f"{v:+.{d}f}" if signed else f"{v:.{d}f}"
    return f"{v:+d}" if signed else str(v)


def table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def by_point(points):
    """{(mode, rate, arm): [point, ...]} over seeds; clean is (clean, 0.0)."""
    out = defaultdict(list)
    for p in points:
        if p.get("databases"):
            continue          # partial timing runs are not reported
        out[(p["mode"], p["rate"], p["arm"])].append(p)
    return out


def g(ps, graph, key):
    return avg([p["graphs"][graph][key] for p in ps])


def report(dataset, points, csv_rows):
    P = by_point(points)
    if not P:
        return f"## {dataset}\n\nNo complete points yet.\n"
    modes = sorted({m for m, _, _ in P if m != "clean"})
    clean = P.get(("clean", 0.0, "full"), [])
    lines = [f"## {dataset}", ""]

    lines += ["### 1. Degradation (FULL)", ""]
    head = ["Mode", "Rate", "Seeds"] + [f"{gr} {k}" for gr in GRAPHS for k in ("P", "R", "F1", "Edges", "FP")]
    rows = []
    for mode in (["clean"] if clean else []) + modes:
        for rate in sorted({r for m, r, a in P if m == mode and a == "full"}):
            ps = P[(mode, rate, "full")]
            rows.append([mode, f"{rate:g}", len(ps)] + [f(g(ps, gr, k), 3 if k in ("precision", "recall", "f1") else 1)
                                                       for gr in GRAPHS for k in ("precision", "recall", "f1",
                                                                                   "predicted", "false_positives")])
    lines += [table(head, rows), ""]

    lines += ["### 2. Distance from thresholds (FULL)", "",
              "Near = within 5 points of the cutoff that decided it (G6: within 0.05). `fn_gate:*` counts gold joins "
              "a gate removed before scoring.", ""]
    keys = sorted({k for p in points if p["arm"] == "full" for gr in GRAPHS for k in p["margins"].get(gr, {})})
    head = ["Mode", "Rate", "Graph"] + keys
    rows = []
    for mode in (["clean"] if clean else []) + modes:
        for rate in sorted({r for m, r, a in P if m == mode and a == "full"}):
            ps = P[(mode, rate, "full")]
            for gr in GRAPHS:
                rows.append([mode, f"{rate:g}", gr] + [f(avg([p["margins"][gr].get(k, 0) for p in ps]), 1) for k in keys])
    lines += [table(head, rows), ""]

    lines += ["### 3. Sparsity-dependent thresholds (arm − FULL)", ""]
    adapt_arms = sorted({a for _, _, a in P if a.startswith("adapt_")})
    head = ["Mode", "Rate"] + [f"{a} Δ{gr} F1 / ΔFP" for a in adapt_arms for gr in GRAPHS]
    rows = []
    for mode in (["clean"] if clean else []) + modes:
        for rate in sorted({r for m, r, a in P if m == mode and a == "full"}):
            base = P[(mode, rate, "full")]
            row = [mode, f"{rate:g}"]
            for a in adapt_arms:
                ps = P.get((mode, rate, a), [])
                for gr in GRAPHS:
                    if not ps:
                        row.append("–")
                        continue
                    df = g(ps, gr, "f1") - g(base, gr, "f1")
                    dfp = g(ps, gr, "false_positives") - g(base, gr, "false_positives")
                    row.append(f"{df:+.3f} / {dfp:+.0f}")
            rows.append(row)
    lines += [table(head, rows), ""]

    lines += ["### 4. Ablations (arm − FULL)", ""]
    arms = [a for a in dict.fromkeys(a for _, _, a in P) if a != "full" and not a.startswith("adapt_")]
    head = ["Arm"] + [f"clean Δ{gr} F1" for gr in GRAPHS] + [f"clean Δ{gr} FP" for gr in GRAPHS] + \
           [f"sparse Δ{gr} F1" for gr in GRAPHS] + [f"sparse Δ{gr} FP" for gr in GRAPHS]
    rows = []
    sparse_pts = [(m, r) for m, r, a in P if m != "clean" and a == "full"]
    for a in arms:
        row = [a]
        for scope in ("clean", "sparse"):
            pts = [("clean", 0.0)] if scope == "clean" else sparse_pts
            for k in ("f1", "false_positives"):
                for gr in GRAPHS:
                    ds = [g(P[(m, r, a)], gr, k) - g(P[(m, r, "full")], gr, k)
                          for m, r in pts if P.get((m, r, a)) and P.get((m, r, "full"))]
                    row.append(f(avg(ds), 3 if k == "f1" else 1, signed=True))
        rows.append(row)
    lines += [table(head, rows), "", "Sparse = mean over every sparse point that has both arms.", ""]

    lines += ["### 5. Sparsity signals (mean over databases)", ""]
    head = ["Mode", "Rate", "orphan_level", "null_level", "thin_level"]
    rows = []
    for mode in (["clean"] if clean else []) + modes:
        for rate in sorted({r for m, r, a in P if m == mode and a == "full"}):
            ps = P[(mode, rate, "full")]
            vals = {k: avg([s[k] for p in ps for s in p["signals"].values()]) for k in head[2:]}
            rows.append([mode, f"{rate:g}"] + [f(vals[k]) for k in head[2:]])
    lines += [table(head, rows), ""]

    for (mode, rate, arm), ps in P.items():
        for gr in GRAPHS:
            csv_rows.append({"dataset": dataset, "mode": mode, "rate": rate, "arm": arm, "graph": gr, "seeds": len(ps),
                             **{k: g(ps, gr, k) for k in ("precision", "recall", "f1", "predicted", "false_positives")}})
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datasets", nargs="+", required=True)
    ap.add_argument("--csv", default=str(OUT / "summary.csv"))
    args = ap.parse_args()
    csv_rows = []
    print("# Sparsity sweep results (generated by evals/sparsity/report.py)\n")
    for d in args.datasets:
        print(report(d, read_jsonl(OUT / d / "points.jsonl"), csv_rows))
    if csv_rows:
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(csv_rows[0]))
            w.writeheader()
            w.writerows(csv_rows)


if __name__ == "__main__":
    main()
