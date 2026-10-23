"""Collects results/ into one markdown table per dataset: paper vs ours, and the
documented -> undocumented gap. Gaps use the fixed-gold PR@15 (see run.py)."""

import argparse
import json

from baselines.joingr import DATASETS, RESULTS

# JoinGR Tables 2, 3 and 5: (R@5, R@15, PR@5, PR@15)
PAPER = {
    ("beaver", "bge", "vanilla"): (0.476, 0.697, 0.124, 0.339),
    ("beaver", "bge", "heuristic"): (0.423, 0.759, 0.058, 0.405),
    ("beaver", "bge", "trained"): (0.553, 0.875, 0.157, 0.645),
    ("beaver", "minilm", "vanilla"): (0.385, 0.667, 0.066, 0.273),
    ("beaver", "minilm", "heuristic"): (0.414, 0.779, 0.074, 0.430),
    ("beaver", "minilm", "trained"): (0.528, 0.873, 0.182, 0.653),
    ("bird", "bge", "vanilla"): (0.918, 0.983, 0.830, 0.964),
    ("bird", "bge", "heuristic"): (0.789, 0.951, 0.650, 0.921),
    ("bird", "bge", "trained"): (0.934, 0.973, 0.877, 0.961),
    ("bird", "minilm", "vanilla"): (0.886, 0.970, 0.775, 0.936),
    ("bird", "minilm", "heuristic"): (0.858, 0.945, 0.762, 0.917),
    ("bird", "minilm", "trained"): (0.909, 0.966, 0.839, 0.951),
    ("spider", "bge", "vanilla"): (0.745, 0.752, 0.729, 0.741),
    ("spider", "bge", "heuristic"): (0.715, 0.747, 0.699, 0.736),
    ("spider", "bge", "trained"): (0.737, 0.747, 0.722, 0.736),
    ("spider", "minilm", "vanilla"): (0.749, 0.752, 0.735, 0.740),
    ("spider", "minilm", "heuristic"): (0.739, 0.748, 0.726, 0.737),
    ("spider", "minilm", "trained"): (0.747, 0.748, 0.735, 0.737),
}

SETTINGS = ["documented", "no_desc", "stripped", "obfuscated"]
METHODS = ["vanilla", "heuristic", "trained"]
METRICS = [(5, "R"), (15, "R"), (5, "PR"), (15, "PR")]


def load(dataset):
    found = {}
    for path in (RESULTS / dataset).glob("*/*/*.json"):
        r = json.loads(path.read_text())
        found[(r["setting"], r["encoder"], r["method"])] = r
    return found


def table_pr15(r):
    return r["summary_fixed"]["15"]["PR"]


def column_r15(r):
    return r["summary_columns"]["15"]["R"] if r.get("summary_columns") else None


def best(res, setting, enc, metric=table_pr15):
    runs = [(metric(r), m) for (s, e, m), r in res.items() if s == setting and e == enc and metric(r) is not None]
    return max(runs) if runs else None


def headline(datasets, metric=table_pr15):
    """Best documented vs best stripped/obfuscated per dataset: the loss DECODE-JOIN has to recover."""
    lines = [
        "| dataset | encoder | documented (best) | stripped (best) | gap | obfuscated (best) | gap |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in datasets:
        res = load(d)
        for enc in sorted({e for _, e, _ in res}):
            doc = best(res, "documented", enc, metric)
            if not doc:
                continue
            row = f"| {d} | {enc} | {doc[0]:.3f} ({doc[1]}) |"
            for setting in ("stripped", "obfuscated"):
                b = best(res, setting, enc, metric)
                row += f" {b[0]:.3f} ({b[1]}) | {b[0] - doc[0]:+.3f} |" if b else " | |"
            lines.append(row)
    return "\n".join(lines)


def fmt(vals):
    return " | ".join(f"{v:.3f}" for v in vals)


def table(dataset):
    res = load(dataset)
    if not res:
        return None
    encoders = sorted({e for _, e, _ in res})
    lines = [
        "| encoder | method | setting | R@5 | R@15 | PR@5 | PR@15 | PR@15 fixed gold | Δ vs documented | col R@15 | col PR@15 |",
        "|" + "---|" * 11,
    ]
    for enc in encoders:
        for method in METHODS:
            paper = PAPER.get((dataset, enc, method))
            if paper:
                lines.append(f"| {enc} | {method} | *paper* | {fmt(paper)} | | | | |")
            base = res.get(("documented", enc, method))
            for setting in SETTINGS:
                r = res.get((setting, enc, method))
                if not r:
                    continue
                vals = [r["summary"][str(k)][m] for k, m in METRICS]
                fixed = r["summary_fixed"]["15"]["PR"]
                delta = f"{fixed - base['summary_fixed']['15']['PR']:+.3f}" if base and setting != "documented" else ""
                c = r.get("summary_columns")
                cols = f"{c['15']['R']:.3f} | {c['15']['PR']:.3f}" if c else " | "
                lines.append(f"| {enc} | {method} | {setting} | {fmt(vals)} | {fixed:.3f} | {delta} | {cols} |")
    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", nargs="+", default=list(DATASETS))
    args = p.parse_args()

    parts = [
        "# Task 1 baselines",
        "PR@15 with fixed gold labels. Heuristic JoinGR returns nothing without join edges, "
        "so on stripped and obfuscated graphs the best method is dense retrieval. "
        "Obfuscated keeps table names, so small schemas (Spider, KaggleDBQA) barely move.",
        headline(args.dataset),
        "## Columns\n\nColumn R@15: columns ranked by query similarity inside each method's top-15 tables, "
        "against gold columns parsed from the gold SQL (gold/<dataset>.json).",
        headline(args.dataset, column_r15),
    ]
    for d in args.dataset:
        t = table(d)
        if t:
            parts.append(f"## {d}\n\n{t}")
    md = "\n\n".join(parts) + "\n"
    out = RESULTS / "summary.md"
    out.write_text(md)
    print(md)
    print(f"-> {out}")
