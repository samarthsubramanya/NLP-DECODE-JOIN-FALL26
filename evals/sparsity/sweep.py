"""Sparsity sweep (hp): sparsify -> Stage 1 -> Stage 2a -> Stage 2b under every arm -> grade + threshold margins.

No LLM. Each point (dataset, mode, rate, seed) builds a sparse copy of the DuckDB file in a scratch folder
(sparsify.py), profiles it in-process with the unchanged profile_columns.profile_database, scores keys with
primary_keys.score_database (--no-names for obfuscated, frozen rules) and joins with foreign_keys.score_database
under each arm (arms.py: FULL, one-component ablations, sparsity-dependent thresholds). Two graphs are graded:
    direct  heuristics only (auto-accepted)      stats  no LLM (band accepted)
The LLM graph needs Stage 3; see run_llm_point.sh for one sparse point.

    python evals/sparsity/sweep.py --datasets bird_stripped fiben_stripped --modes hard row_sample --rates 0.3 0.5 --seeds 0
    python evals/sparsity/sweep.py --datasets spider_stripped --modes clean          # rate-0 baseline only

Writes (nothing outside out/sparsity/, and every existing out/ run is left alone):
    out/sparsity/<dataset>/points.jsonl                  one line per point x arm: P/R/F1, edges, false positives,
                                                         band size, sparsity signals, threshold-margin summary
    out/sparsity/<dataset>/<tag>/profiles/<db>.json      Stage 1 of the sparse copy (reused on a rerun)
    out/sparsity/<dataset>/<tag>/edges_<arm>.jsonl       one row per candidate with its margins (--edges arms)
The sparse copy itself is deleted after profiling unless --keep-copies.
A rate-0 "clean" point is always run first for each dataset, so every curve has its baseline.
"""
import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

import common
from common import OUT, Gold, append_jsonl, prf, read_jsonl, source_file, uses_names, work_list

import arms as A
import margins as M
import sparsify as S


def tag_of(mode, rate, seed):
    return "clean" if mode == "clean" or rate == 0 else f"{mode}_r{rate:g}_s{seed}"


def profile_point(dataset, mode, rate, seed, point_dir, scratch, keep_copies, databases, log):
    """{db_id: profile} for one point: cached files if complete, else sparsify + profile."""
    import duckdb
    from profile_columns import DEFAULTS as PROFILE_DEFAULTS, profile_database
    dbs = work_list(dataset, databases)
    pdir = point_dir / "profiles"
    cached = {d["db_id"]: pdir / f"{d['db_id']}.json" for d in dbs}
    if all(p.exists() for p in cached.values()):
        return {db: json.loads(p.read_text()) for db, p in cached.items()}, None
    t0 = time.time()
    copy_dir = None
    if mode == "clean" or rate == 0:
        path, plans = source_file(dbs[0]["duckdb_file"]), None
    else:
        copy_dir = Path(scratch) / dataset / tag_of(mode, rate, seed)
        built = S.build(dataset, copy_dir, mode, rate, seed, databases, log)
        path, plans = built["file"], built["plans"]
    con = duckdb.connect(str(path), read_only=True)
    pdir.mkdir(parents=True, exist_ok=True)
    out = {}
    for d in dbs:
        prof = profile_database(con, d["duckdb_schema"], d["tables"], PROFILE_DEFAULTS, d["db_id"], log=log)
        prof.update(dataset=dataset, duckdb_file=d["duckdb_file"], sparsity={"mode": mode, "rate": rate, "seed": seed})
        if plans:
            prof["sparsity"]["gold_edges_changed"] = plans[d["db_id"]].get("_gold_edges")
        cached[d["db_id"]].write_text(json.dumps(prof, sort_keys=True, default=str))
        out[d["db_id"]] = json.loads(cached[d["db_id"]].read_text())   # same types as a cached load
    con.close()
    if copy_dir is not None and not keep_copies:
        shutil.rmtree(copy_dir, ignore_errors=True)
    return out, round(time.time() - t0, 1)


def keys_for(profile, dataset):
    import primary_keys as pk
    return pk.score_database(profile, {**pk.DEFAULTS, "use_names": uses_names(dataset)})


def grade_point(dataset, gold, profiles, keys_by_db, arm, fk_cfg, edges_path=None, extra=None):
    """Totals over the databases of one point under one arm, plus the margin summary."""
    totals = {g: [0, 0, 0] for g in ("direct", "stats")}
    rows, counts, per_db, sig = [], {}, {}, {}
    use_names = uses_names(dataset)
    for db, prof in profiles.items():
        joins = A.run_arm(arm, prof, keys_by_db[db], fk_cfg, use_names)
        if joins is None:
            return None
        fks = gold.pairs(db, prof)
        norm = lambda e, db=db: gold.norm(db, e)   # noqa: E731
        per_db[db] = {}
        for g, t in totals.items():
            edges = {norm(e) for e in joins["graphs"][g]}
            tp = len(edges & fks)
            t[0] += tp
            t[1] += len(edges)
            t[2] += len(fks)
            per_db[db][g] = prf(tp, len(edges), len(fks))
        for k, v in joins["counts"].items():
            counts[k] = counts.get(k, 0) + v
        if joins["config"].get("sparsity_signals"):
            sig[db] = {"signals": joins["config"]["sparsity_signals"],
                       "cfg": {k: joins["config"][k] for k in ("containment_min", "orphan_max", "null_low",
                                                                "null_high", "delta")}}
        rows += M.rows_for(db, joins, fks, norm, joins["config"], extra)
    if edges_path:
        if edges_path.exists():
            edges_path.unlink()
        append_jsonl(edges_path, rows)
    return {"graphs": {g: prf(*t) for g, t in totals.items()}, "counts": counts,
            "margins": M.summarize(rows, ("direct", "stats")), "adaptive": sig or None, "per_db": per_db}


def signals_of(profiles, keys_by_db):
    """Sparsity signals per database, measured on every point (also for arms that do not use them)."""
    import foreign_keys as fk
    from adaptive import signals
    return {db: signals(p, keys_by_db[db], fk.DEFAULTS) for db, p in profiles.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datasets", nargs="+", required=True, help="e.g. bird_stripped fiben_obfuscated")
    ap.add_argument("--modes", nargs="+", default=["hard"], choices=("clean", *S.MODES))
    ap.add_argument("--rates", nargs="+", type=float, default=[0.1, 0.3, 0.5, 0.7])
    ap.add_argument("--seeds", nargs="+", type=int, default=[0])
    ap.add_argument("--arms", nargs="+", default=list(A.ARMS), help=f"default: all of {list(A.ARMS)}")
    ap.add_argument("--edges", nargs="*", default=list(A.CORE), help="arms whose per-candidate rows are saved")
    ap.add_argument("--databases", nargs="*", help="only these db_ids (for a quick timing run)")
    ap.add_argument("--scratch", default=str(Path(tempfile.gettempdir()) / "decode_join_sparse"))
    ap.add_argument("--keep-copies", action="store_true", help="keep the sparse DuckDB files (for run_llm_point.sh)")
    ap.add_argument("--force", action="store_true", help="redo points already in points.jsonl")
    args = ap.parse_args()
    import foreign_keys as fk
    fk_cfg = {**fk.DEFAULTS, "keys_run": "sparsity_sweep"}

    for dataset in args.datasets:
        gold = Gold(dataset)
        out_dir = OUT / dataset
        points_path = out_dir / "points.jsonl"
        done = {(p["mode"], p["rate"], p["seed"], p["arm"], tuple(p.get("databases") or ()))
                for p in read_jsonl(points_path)}
        grid = [("clean", 0.0, 0)] + [(m, r, s) for m in args.modes if m != "clean"
                                       for r in args.rates if r > 0 for s in args.seeds]
        for mode, rate, seed in grid:
            dbs_key = tuple(args.databases or ())
            todo = [a for a in args.arms if args.force or (mode, rate, seed, a, dbs_key) not in done]
            if not todo:
                continue
            tag = tag_of(mode, rate, seed)
            point_dir = out_dir / tag
            print(f"== {dataset} {tag}")
            profiles, secs = profile_point(dataset, mode, rate, seed, point_dir, args.scratch, args.keep_copies,
                                           args.databases, print)
            keys_by_db = {db: keys_for(p, dataset) for db, p in profiles.items()}
            sig = signals_of(profiles, keys_by_db)
            for arm in todo:
                t0 = time.time()
                res = grade_point(dataset, gold, profiles, keys_by_db, arm, fk_cfg,
                                  point_dir / f"edges_{arm}.jsonl" if arm in (args.edges or ()) else None,
                                  {"dataset": dataset, "tag": tag, "arm": arm})
                if res is None:
                    continue
                line = {"dataset": dataset, "mode": mode, "rate": rate, "seed": seed, "arm": arm, "tag": tag,
                        "databases": list(dbs_key), "profile_seconds": secs, "score_seconds": round(time.time() - t0, 1),
                        "signals": sig, **res}
                append_jsonl(points_path, [line])
                g = res["graphs"]
                print(f"  {arm:22s} direct F1 {g['direct']['f1']:.3f} FP {g['direct']['false_positives']:4d} | "
                      f"stats F1 {g['stats']['f1']:.3f} FP {g['stats']['false_positives']:4d} "
                      f"band {res['counts'].get('band', 0)}")


if __name__ == "__main__":
    main()
