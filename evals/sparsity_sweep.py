"""Sparsity sweep (rq3_plan Step 2, mentor 2026-11-11: make the existing datasets as sparse as possible and
measure how far the method sits from its thresholds). No LLM; nothing is written to out/ except the results.

Each database is copied into an in-memory DuckDB, one perturbation is applied, then Stage 1, 2a and 2b run and
graphs.direct (accepted) and graphs.stats (accepted + LLM band) are graded against the declared FKs.

Modes (gold picks the columns, the pipeline never sees it; rows chosen by hash(rowid, seed)):
    clean       no change
    pk_null     null max(1, rate x rows) rows of every gold FK target column
    fk_null     null max(1, rate x rows) rows of every gold FK source column
    row_sample  keep ceil(rate x rows) rows of every table, independently

Variants, scored on the same profile:
    strict      quartile's rule: a key target has no nulls (dense picks dropped, empty-or-zero reject at .5)
    tolerant    null_check's rule: a table's 5 densest key-family columns may be keys if unique among non-nulls

    cap         suggestion 2a: every key-family column with null rate <= 0.2 may be a key (no top-5 race)
    cap_adapt   2a + 2b: containment into an int target is divided by the target's id density
                (floor .5), so a parent missing rows is not gated by G6 / the orphan penalty

Threshold distance: for every gold FK, the best record's decision and its score minus the accept cutoff (60).
"missing" = no containment pair at all (the target was not key-like, or containment fell under .5).

Usage (from the repo root):
    python evals/sparsity_sweep.py --datasets spider bird fiben spider_obfuscated bird_obfuscated fiben_obfuscated
Writes out/eval/sparsity/sweep.jsonl (one line per dataset x db x mode x rate x variant).
"""
import argparse
import copy
import json
import math
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "evals")]
import foreign_keys as fk                       # noqa: E402
import labels                                   # noqa: E402
import primary_keys as pk                       # noqa: E402
from duckdb_driver import LOCAL_DIR             # noqa: E402
from grade_beaver import prf                    # noqa: E402
from grade_beaver import query_joins            # noqa: E402
from grade_joins_all import load_gold           # noqa: E402
from profile_columns import DEFAULTS as P_DEFAULTS, key_like, mark_dense_picks, profile_database, q  # noqa: E402

POINTS = [("clean", 0)] + [("pk_null", r) for r in (0.001, 0.01, 0.1)] + \
         [("fk_null", r) for r in (0.5, 0.9)] + [("row_sample", r) for r in (0.5, 0.1)]
RANK = {"accepted": 0, "band": 1, "rejected_score": 2, "rejected_gate": 3}
OUT = ROOT / "out/eval/sparsity/sweep.jsonl"


def gold_questions(base):
    """Our gold set's questions (baselines/gold_columns.py) in the form grade_beaver.query_joins reads."""
    import re
    import sqlglot
    from baselines.gold_columns import DIALECTS, DOUBLE_QUOTED_STRINGS, SQL_FIELDS
    from baselines.joingr import load_source_graph
    out = []
    for q_ in load_source_graph(base)["queries"]:
        sql = next((q_[f] for f in SQL_FIELDS if q_.get(f)), "")
        if base in DOUBLE_QUOTED_STRINGS:
            sql = re.sub(r'"([^"]*)"', r"'\1'", sql)
        try:
            sqlglot.parse_one(sql, read=DIALECTS[base])
        except sqlglot.errors.SqlglotError:      # query_joins only catches ParseError
            continue
        out.append({"db_id": (q_.get("db_id") or "dw").lower(), "gold_sql": sql, "dialect": DIALECTS[base]})
    return out


def orient(pair, prof):
    """(source, target) of an unordered gold pair, in profile names: the unique end is the target; two unique
    ends: the table with more rows is the target. None if a column is missing from the profile."""
    a, b = sorted(pair, key=lambda x: (x[0], x[1]))
    pa, pb = (prof["tables"].get(x[0], {}).get("columns", {}).get(x[1]) for x in (a, b))
    if pa is None or pb is None:
        return None
    uniq = [p["rows"] > p["nulls"] and p["distinct"] == p["rows"] - p["nulls"] for p in (pa, pb)]
    rows = [prof["tables"][x[0]]["rows"] for x in (a, b)]
    if uniq[0] and (not uniq[1] or rows[0] > rows[1]):
        return b, a
    return a, b


def shown_names(prof, mapping, db):
    """{(table lower, real column lower): (table, column as in the data)}."""
    if mapping:
        return {(t.lower(), c.lower()): (t, s) for t, cols in mapping[db]["columns"].items() for c, s in cols.items()}
    return {(t.lower(), c.lower()): (t, c) for t, v in prof["tables"].items() for c in v["columns"]}


def copy_db(src_file, schema, tables):
    con = duckdb.connect()
    con.execute(f"ATTACH '{Path(LOCAL_DIR) / src_file}' AS src (READ_ONLY)")
    con.execute("CREATE SCHEMA s")
    for t in tables:
        con.execute(f"CREATE TABLE s.{q(t)} AS SELECT * FROM src.{q(schema)}.{q(t)}")
    con.execute("DETACH src")
    return con


def pick(table, n, seed):
    return f"SELECT rowid FROM s.{q(table)} ORDER BY hash(rowid, {int(seed)}) LIMIT {int(n)}"


def perturb(con, mode, rate, cols, tables, seed):
    if mode in ("pk_null", "fk_null"):
        for t, c in cols:
            rows = con.execute(f"SELECT count(*) FROM s.{q(t)}").fetchone()[0]
            if rows:
                con.execute(f"UPDATE s.{q(t)} SET {q(c)} = NULL WHERE rowid IN ({pick(t, max(1, round(rate * rows)), seed)})")
    elif mode == "row_sample":
        for t in tables:
            rows = con.execute(f"SELECT count(*) FROM s.{q(t)}").fetchone()[0]
            con.execute(f"DELETE FROM s.{q(t)} WHERE rowid NOT IN ({pick(t, max(1, math.ceil(rate * rows)), seed)})")


def variant_profile(prof, n=0, null_max=None):
    """The profile as Stage 1 would write it under one dense-pick rule. `prof` was profiled with every
    key-family column a dense pick, so its containment pairs cover every candidate target; the flags are
    re-marked (n = top-n rule, null_max = null-rate cap, neither = quartile's strict rule) and the pairs
    filtered to the targets that are key-like under that rule."""
    p = copy.deepcopy(prof)
    for t in p["tables"].values():
        for c in t["columns"].values():
            c.pop("dense_pick", None)
        mark_dense_picks(t, n, null_max)
    ok = {(t, c) for t, v in p["tables"].items() for c, x in v["columns"].items() if key_like(x)}
    p["containment"] = [r for r in p["containment"] if tuple(r["target"]) in ok]
    return p


NULL_MAX, ADAPT_FLOOR = 0.2, 0.5
VARIANTS = [   # name, profile rule, Stage 2a overrides, Stage 2b overrides
    ("strict", {}, {"reject_nulls": True, "empty_or_zero_max": 0.5}, {}),
    ("tolerant", {"n": 5}, {}, {}),
    ("cap", {"null_max": NULL_MAX}, {}, {}),                                       # suggestion 2a
    ("cap_adapt", {"null_max": NULL_MAX}, {}, {"containment_adaptive": ADAPT_FLOOR}),  # 2a + 2b
]


def threshold_view(joins, gold, mapping, db, accept):
    best = {}
    for r in joins["candidates"]:
        e = labels.norm_edge(mapping, db, r)
        if e in gold and (e not in best or RANK[r["decision"]] < RANK[best[e]["decision"]]
                          or (r["decision"] == best[e]["decision"] and r.get("score", 0) > best[e].get("score", 0))):
            best[e] = r
    status = Counter(best[e]["decision"] if e in best else "missing" for e in gold)
    margins = [best[e]["score"] - accept for e in gold if e in best and "score" in best[e]]
    return dict(status), margins


def run_point(con_factory, mode, rate, args, ctx):
    con = con_factory()
    perturb(con, mode, rate, ctx["pk_cols"] if mode == "pk_null" else ctx["fk_cols"], ctx["tables"], args.seed)
    prof = profile_database(con, "s", ctx["tables"], {**P_DEFAULTS, "seed": args.seed, "dense_pick": 10 ** 6},
                            ctx["db"], log=lambda *_: None)
    con.close()
    out = []
    for variant, rule, kcfg, jcfg in VARIANTS:
        if args.variants and variant not in args.variants:
            continue
        p = variant_profile(prof, **rule)
        keys = pk.score_database(p, {**pk.DEFAULTS, "use_names": ctx["use_names"], **kcfg})
        joins = fk.score_database(p, keys, {**fk.DEFAULTS, "keys_run": variant, **jcfg})
        row = {"dataset": ctx["dataset"], "db": ctx["db"], "mode": mode, "rate": rate, "variant": variant,
               "seed": args.seed, "gold_source": ctx["gold_source"], "counts": joins["counts"], "gold": len(ctx["gold"])}
        for g in ("direct", "stats"):
            edges = {labels.norm_edge(ctx["mapping"], ctx["db"], e) for e in joins["graphs"][g]}
            row[g] = {"tp": len(edges & ctx["gold"]), "predicted": len(edges)}
        row["gold_status"], row["margins"] = threshold_view(joins, ctx["gold"], ctx["mapping"], ctx["db"],
                                                            fk.DEFAULTS["accept"])
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datasets", nargs="+", required=True)
    ap.add_argument("--databases", nargs="*", help="only these db_ids")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--variants", nargs="*", help="only these variants (default: all)")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--gold-sql", action="store_true",
                    help="grade against the join pairs in our gold SQL (gold/<dataset>.json questions) instead of "
                         "declared FKs; recall-only label, so precision is a lower bound")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    for dataset in args.datasets:
        base = labels.documented(dataset)
        schemas, mapping, gold_of = load_gold(dataset)
        sql_questions = gold_questions(base) if args.gold_sql else []
        for d in json.loads((ROOT / f"out/filtered/{dataset}/databases.json").read_text()):
            db = d["db_id"]
            if args.databases and db not in args.databases:
                continue
            clean = json.loads((ROOT / f"out/profiles/{dataset}/{db}.json").read_text())
            gold = set(query_joins(sql_questions, db.lower())) if args.gold_sql else set(gold_of(db))
            if base == "fiben":   # as grade_joins_all: only FKs whose two tables have rows
                rows = {t.lower(): v["rows"] for t, v in clean["tables"].items()}
                gold = {f for f in gold if all(rows.get(t, 0) for t, _ in f)}
            if not gold:
                continue
            names = shown_names(clean, mapping, db)
            oriented = [orient([names[x] for x in pair], clean) for pair in gold if all(x in names for x in pair)]
            oriented = [o for o in oriented if o]
            ctx = {"gold_source": "gold_sql" if args.gold_sql else "declared", "dataset": dataset, "db": db, "tables": d["tables"], "gold": gold, "mapping": mapping,
                   "use_names": not dataset.endswith("_obfuscated"),
                   "pk_cols": sorted({t for _, t in oriented}), "fk_cols": sorted({s for s, _ in oriented})}
            start = time.time()
            with out.open("a") as f:
                for mode, rate in POINTS:
                    for row in run_point(lambda: copy_db(d["duckdb_file"], d["duckdb_schema"], d["tables"]),
                                         mode, rate, args, ctx):
                        f.write(json.dumps(row) + "\n")
            print(f"{dataset} {db}: {len(gold)} gold FKs, {time.time() - start:.0f}s", flush=True)


def summary(path=OUT):
    """{(dataset, mode, rate, variant): stats P/R/F1, gold status counts, median margin} summed over databases."""
    agg = {}
    for line in Path(path).read_text().splitlines():
        r = json.loads(line)
        a = agg.setdefault((r["dataset"], r["mode"], r["rate"], r["variant"]),
                           {"tp": 0, "predicted": 0, "gold": 0, "status": Counter(), "margins": []})
        a["tp"] += r["stats"]["tp"]
        a["predicted"] += r["stats"]["predicted"]
        a["gold"] += r["gold"]
        a["status"].update(r["gold_status"])
        a["margins"] += r["margins"]
    return {k: {**prf(a["tp"], a["predicted"], a["gold"]), "status": dict(a["status"]),
                "median_margin": statistics.median(a["margins"]) if a["margins"] else None}
            for k, a in agg.items()}


if __name__ == "__main__":
    main()
