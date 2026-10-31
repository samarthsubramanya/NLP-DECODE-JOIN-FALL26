"""Memorization check: perturb the values Stage 3 shows the LLM, keeping every statistic Stages 1-2b used.

Stage 3 sees values only through the Stage 1 profiles (top_values, sample_values, sample_rows,
date_range), so this rewrites the profiles of a scratch copy in place (e.g. one made by
evals/hide_tables.py). The keys and joins files stay valid: containment, uniqueness, distinct counts,
null rates, density and lengths are unchanged.

Levels:
    dates  date columns move 56 years forward (a multiple of 28: weekdays and leap days stay aligned).
           Date columns are never in a Stage 1 containment pair, so no overlap changes.
    all    dates, plus text values pass through one substitution cipher per database on letters and
           digits (case, length and punctuation kept): words and codes become unreadable. A text key
           and the columns pointing at it get the same mapping, so containment is exact.
Integers are kept. Shifting key domains consistently would also shift every measure whose values fall
inside a key's range (a 12-60 month duration inside district ids 1-77), destroying its meaning, and
deciding which overlaps are real joins would need the gold labels.

Usage: python evals/perturb_values.py <root> <dataset> --level dates|all [--seed 0]
Writes <root>/perturbation.json (the shift and each database's cipher) so a run can be audited.
"""
import argparse
import json
import random
import re
import string
from pathlib import Path

DATE_SHIFT_YEARS = 56
DATE_RE = re.compile(r"^(\d{4})(-\d{2}-\d{2}.*)$")


def cipher(rng):
    lo, dg = list(string.ascii_lowercase), list(string.digits)
    rng.shuffle(lo)
    rng.shuffle(dg)
    return (dict(zip(string.ascii_lowercase, lo)) | dict(zip(string.ascii_uppercase, (c.upper() for c in lo)))
            | dict(zip(string.digits, dg)))


def shift_date(v):
    m = DATE_RE.match(v) if isinstance(v, str) else None
    return f"{int(m[1]) + DATE_SHIFT_YEARS}{m[2]}" if m else v


def perturb(profile, level, rng):
    """Rewrites the profile in place; returns what was done."""
    sub = cipher(rng) if level == "all" else None
    enc = lambda v: "".join(sub.get(ch, ch) for ch in v) if isinstance(v, str) else v
    for tp in profile["tables"].values():
        for c, cp in tp["columns"].items():
            f = shift_date if cp["family"] == "date" else enc if sub and cp["family"] == "text" else None
            if not f:
                continue
            cp["sample_values"] = [f(v) for v in cp.get("sample_values", [])]
            cp["top_values"] = [[f(v), n] for v, n in cp.get("top_values", [])]
            if cp.get("date_range"):
                cp["date_range"] = [shift_date(v) for v in cp["date_range"]]
            for row in tp["sample_rows"]:
                if c in row:
                    row[c] = f(row[c])
    return {"date_shift_years": DATE_SHIFT_YEARS,
            "cipher": "".join(sub[ch] for ch in string.ascii_lowercase + string.digits) if sub else None}


def _selfcheck():
    """Dates shift with leap days intact; text is a length- and case-preserving bijection; ints untouched."""
    col = lambda fam, vals, **x: {"family": fam, "sample_values": vals, "top_values": [[v, 1] for v in vals], **x}
    prof = {"tables": {"a": {"columns": {"id": col("int", ["5"]), "name": col("text", ["Alameda"]),
                                         "d": col("date", ["1996-02-29 00:00:00"], date_range=["1996-02-29", "1996-03-01"])},
                             "sample_rows": [{"id": 5, "name": "Alameda", "d": "1996-02-29"}]}}}
    log = perturb(prof, "all", random.Random(0))
    a = prof["tables"]["a"]
    assert a["columns"]["d"]["sample_values"] == ["2052-02-29 00:00:00"] and a["columns"]["d"]["date_range"][1] == "2052-03-01"
    name = a["sample_rows"][0]["name"]
    assert len(name) == 7 and name[0].isupper() and name != "Alameda" and name == a["columns"]["name"]["sample_values"][0]
    assert a["sample_rows"][0]["id"] == 5 and len(set(log["cipher"][:26])) == 26


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("dataset")
    ap.add_argument("--level", choices=["dates", "all"], required=True)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    _selfcheck()
    root, out = Path(args.root), {"level": args.level, "seed": args.seed, "databases": {}}
    for i, p in enumerate(sorted((root / "out/profiles" / args.dataset).glob("*.json"))):
        prof = json.loads(p.read_text())
        out["databases"][p.stem] = perturb(prof, args.level, random.Random(args.seed * 1000 + i))
        p.write_text(json.dumps(prof, indent=1, ensure_ascii=False))
    (root / "perturbation.json").write_text(json.dumps(out, indent=1))
    print("ok", args.level, len(out["databases"]), "databases")


if __name__ == "__main__":
    main()
