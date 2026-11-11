"""Build ablation variants WITHOUT editing any shared pipeline code. Every output goes to a new run folder.

Two commands:

  no-qmd      Stage 2b with the quantile-EMD distribution term removed (A1, "-QuantileMD").
              Loads the cached Stage 1 profiles, drops `dist` / `dist_buckets` from every containment pair
              IN MEMORY (the files in out/profiles/ are not touched), then calls the unchanged
              foreign_keys.score_database. With no distribution evidence, Stage 2b already moves the
              distribution points to overlap and skips the low-distribution cap, so this is exactly
              "the same scorer, minus QuantileMD". Takes every foreign_keys.py option.

      python evals/ablation/variants.py no-qmd --dataset fiben_stripped --keys-run ddb --run abl_noqmd

  swap-graph  Copy a finished Stage 3 run to a new run whose graphs.llm is replaced by another graph, so the
              unchanged export_joingr.py exports it (A2 "-LLM verification" = stats, A4 "heuristics only" = direct).
              The original final graph is kept as graphs.llm_full for reference.

      python evals/ablation/variants.py swap-graph --dataset fiben_stripped --from-run ddb_low --graph stats --run abl_nollm

Both refuse to write into a folder that already exists, so no existing run can be overwritten.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src")]


def strip_distribution(profile):
    """A copy of a Stage 1 profile with no quantile-EMD evidence on any containment pair."""
    out = dict(profile)
    out["containment"] = [{**p, "dist": None, "dist_buckets": None} for p in profile.get("containment", [])]
    return out


def fresh_dir(path):
    if path.exists():
        sys.exit(f"{path.relative_to(ROOT)} already exists; pick a new --run name (existing runs are never overwritten)")
    path.mkdir(parents=True)
    return path


def no_qmd(argv):
    import foreign_keys as fk
    from primary_keys import load_profile, work_list
    ap = argparse.ArgumentParser(prog="variants.py no-qmd")
    fk.add_args(ap)
    args = ap.parse_args(argv)
    cfg = fk.config_from(args)
    keys_dir = ROOT / "out" / "keys" / args.dataset / args.keys_run
    out_dir = fresh_dir(ROOT / "out" / "joins" / args.dataset / args.run)
    for db in work_list(args):
        keys_path = keys_dir / f"{db['db_id']}.json"
        if not keys_path.exists():
            sys.exit(f"{keys_path.relative_to(ROOT)} is missing; run Stage 2a with --run {args.keys_run} first")
        overlaps = []
        if cfg["repair_isolated"]:
            overlaps = json.loads((ROOT / "out" / "overlaps" / args.dataset / f"{db['db_id']}.json").read_text())
        profile = strip_distribution(load_profile(args.dataset, db))
        joins = fk.score_database(profile, json.loads(keys_path.read_text()), cfg, overlaps)
        joins["ablation"] = "no_quantile_md"
        c = joins["counts"]
        print(f"  joins {db['db_id']} (no QuantileMD): {c.get('accepted', 0)} accepted, {c.get('band', 0)} band, "
              f"{len(joins['graphs']['stats'])} edges (stats)")
        (out_dir / f"{db['db_id']}.json").write_text(json.dumps(joins, indent=1, sort_keys=True))


def swap(joins, graph):
    if graph not in joins["graphs"]:
        raise KeyError(f"graphs.{graph} is not in this joins file (has {sorted(joins['graphs'])})")
    out = json.loads(json.dumps(joins))
    out["graphs"]["llm_full"] = out["graphs"]["llm"]
    out["graphs"]["llm"] = out["graphs"][graph]
    out["ablation"] = f"graph={graph}"
    return out


def swap_graph(argv):
    ap = argparse.ArgumentParser(prog="variants.py swap-graph")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--from-run", required=True, help="finished Stage 3 run under out/descriptions/<dataset>/")
    ap.add_argument("--graph", required=True, choices=["stats", "direct"])
    ap.add_argument("--run", required=True, help="new run name, e.g. abl_nollm")
    args = ap.parse_args(argv)
    src = ROOT / "out" / "descriptions" / args.dataset / args.from_run
    if not (src / "joins").is_dir():
        sys.exit(f"{src.relative_to(ROOT)}/joins is missing; run Stage 3 first")
    dst = fresh_dir(ROOT / "out" / "descriptions" / args.dataset / args.run)
    for f in src.glob("*.json"):            # descriptions, unchanged
        shutil.copy2(f, dst / f.name)
    (dst / "joins").mkdir()
    for f in sorted((src / "joins").glob("*.json")):
        out = swap(json.loads(f.read_text()), args.graph)
        (dst / "joins" / f.name).write_text(json.dumps(out, indent=1, sort_keys=True))
        print(f"  {f.stem}: graphs.llm <- graphs.{args.graph} ({len(out['graphs']['llm'])} edges, "
              f"full had {len(out['graphs']['llm_full'])})")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("no-qmd", "swap-graph"):
        sys.exit(__doc__)
    {"no-qmd": no_qmd, "swap-graph": swap_graph}[sys.argv[1]](sys.argv[2:])
