"""Saved sparse variants of the 5 benchmarks (hp): real DuckDB files that the team can query and write gold
questions against, next to the documented / stripped / obfuscated files.

Two levels, applied to every table of every database (sparsify.py mode "mix", no gold used):
    medium   30% of rows removed, 10% of cells nulled
    hard     50% of rows removed, 30% of cells nulled
Each level is built for documented, stripped and obfuscated, with rows and cells chosen by row CONTENT and column
POSITION (align="content"), so the three settings of one benchmark hold the same sparse data under different names.
A question written on the documented sparse file is valid for its stripped and obfuscated twins.

Files keep their original names, so the unchanged pipeline reads a variant with one environment variable:
    DECODE_JOIN_DUCKDB_DIR=<out>/<level> python src/profile_columns.py --dataset bird_stripped ...
(do that in a scratch copy of the repo, as evals/run_truncation.sh does, so out/ runs do not mix).

    python evals/sparsity/make_variants.py                                  # both levels, all 5 benchmarks
    python evals/sparsity/make_variants.py --levels medium --benchmarks bird fiben
    python evals/sparsity/make_variants.py --validate-only                  # recheck an existing build

Writes <out>/<level>/<duckdb files>, <out>/<level>/manifest.json (spec, seed, per-table rows and null shares,
cross-setting alignment checks) and <out>/<level>/README.md. Default <out> is ../sparse-variants (outside the repo,
next to JoinGR-E656); nothing in the repo or in the original files is changed. An existing level folder is never
overwritten without --force.
"""
import argparse
import json
import time
from pathlib import Path

import common
from common import BENCHMARKS, source_file, work_list

import sparsify as S

LEVELS = {"medium": {"rows": 0.3, "nulls": 0.1}, "hard": {"rows": 0.5, "nulls": 0.3}}
SETTINGS = ("", "_stripped", "_obfuscated")
SEED = 0
DEFAULT_OUT = common.ROOT.parent / "sparse-variants"


def q(name):
    return S.q(name)


def table_stats(con, catalog, schema, table):
    cols = S.table_columns(con, catalog, schema, table)
    rows = con.execute(f"SELECT count(*) FROM {catalog}.{q(schema)}.{q(table)}").fetchone()[0]
    if not cols or not rows:
        return {"rows": rows, "null_share": None}
    nn = con.execute("SELECT " + " + ".join(f"count({q(c)})" for c in cols) +
                     f" FROM {catalog}.{q(schema)}.{q(table)}").fetchone()[0]
    return {"rows": rows, "null_share": round(1 - nn / (rows * len(cols)), 4)}


def align_check(con, schema, table, other):
    """Rows of the documented sparse table that differ from `other` setting's (compared by position), both ways."""
    try:
        a = con.execute(f"SELECT count(*) FROM (SELECT * FROM doc.{q(schema)}.{q(table)} EXCEPT ALL "
                        f"SELECT * FROM {other}.{q(schema)}.{q(table)})").fetchone()[0]
        b = con.execute(f"SELECT count(*) FROM (SELECT * FROM {other}.{q(schema)}.{q(table)} EXCEPT ALL "
                        f"SELECT * FROM doc.{q(schema)}.{q(table)})").fetchone()[0]
        return a + b
    except Exception as e:   # noqa: BLE001  (types differ between settings)
        return f"not comparable: {str(e)[:80]}"


def validate(level_dir, benchmarks):
    import duckdb
    report = {}
    for base in benchmarks:
        dbs = work_list(base)
        files = {s: work_list(base + s)[0]["duckdb_file"] for s in SETTINGS}
        con = duckdb.connect()
        con.execute(f"ATTACH {S.lit(source_file(files['']))} AS orig (READ_ONLY)")
        for s, alias in (("", "doc"), ("_stripped", "str"), ("_obfuscated", "obf")):
            con.execute(f"ATTACH {S.lit(Path(level_dir) / files[s])} AS {alias} (READ_ONLY)")
        out, mismatched = {}, 0
        for db in dbs:
            schema = db["duckdb_schema"]
            for t in db["tables"]:
                before = table_stats(con, "orig", schema, t)
                after = table_stats(con, "doc", schema, t)
                diff = {o: align_check(con, schema, t, o) for o in ("str", "obf")}
                mismatched += sum(1 for v in diff.values() if v != 0)
                out[f"{db['db_id']}.{t}"] = {"rows_before": before["rows"], "rows_after": after["rows"],
                                             "null_share_before": before["null_share"],
                                             "null_share_after": after["null_share"],
                                             "differs_from_stripped": diff["str"], "differs_from_obfuscated": diff["obf"]}
        con.close()
        kept = [v["rows_after"] / v["rows_before"] for v in out.values() if v["rows_before"]]
        nulls = [v["null_share_after"] for v in out.values() if v["null_share_after"] is not None]
        report[base] = {"tables": len(out), "empty_tables_before": sum(not v["rows_before"] for v in out.values()),
                        "mean_rows_kept": round(sum(kept) / len(kept), 4) if kept else None,
                        "mean_null_share_after": round(sum(nulls) / len(nulls), 4) if nulls else None,
                        "tables_not_aligned": mismatched, "per_table": out}
        print(f"  {base:11s} {len(out):4d} tables, rows kept {report[base]['mean_rows_kept']}, null share "
              f"{report[base]['mean_null_share_after']}, not aligned across settings: {mismatched}")
    return report


README = """# Sparse variant: {level}

Built by `evals/sparsity/make_variants.py` (hp) on {date}. Every table of every database:
{rows:.0%} of rows removed, {nulls:.0%} of cells nulled (seed {seed}), chosen by row content and column position,
so the documented, stripped and obfuscated files below hold the same data under different names.

| File | Benchmarks / settings |
|---|---|
{files}

Writing questions: open the documented file read-only, e.g.
`duckdb -readonly {example}` then `SET search_path = '<duckdb_schema>';` (schema names are in
`out/filtered/<benchmark>/databases.json`). A question written here is valid for the stripped and obfuscated twins.

Running the pipeline on this variant: `DECODE_JOIN_DUCKDB_DIR=<this folder>` in a scratch copy of the repo.
Per-table rows and null shares before/after, and the cross-setting checks: `manifest.json`.
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--levels", nargs="+", default=list(LEVELS), choices=list(LEVELS))
    ap.add_argument("--benchmarks", nargs="+", default=list(BENCHMARKS), choices=list(BENCHMARKS))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--force", action="store_true", help="rebuild a level folder that already has files")
    ap.add_argument("--validate-only", action="store_true")
    args = ap.parse_args()
    for level in args.levels:
        spec = LEVELS[level]
        level_dir = Path(args.out) / level
        if not args.validate_only:
            mine = {work_list(b + s)[0]["duckdb_file"] for b in args.benchmarks for s in SETTINGS}
            there = sorted(f for f in mine if (level_dir / f).exists())
            man = level_dir / "manifest.json"   # a benchmark counts as built once it was validated
            built = set(json.loads(man.read_text())["benchmarks"]) if man.exists() else set()
            done = {b for b in args.benchmarks if b in built}
            if done and not args.force:   # files shared with another benchmark (Spider/BEAVER) get schemas added
                print(f"   skipping {sorted(done)}: already built in {level_dir} (--force to rebuild)")
                args_benchmarks = [b for b in args.benchmarks if b not in done]
            else:
                args_benchmarks = list(args.benchmarks)
                if args.force:
                    for f in there:
                        (level_dir / f).unlink()
            t0 = time.time()
            written = {f for f in there if not args.force}   # existing files get schemas added, not replaced
            for base in args_benchmarks:
                for s in SETTINGS:
                    ds = base + s
                    fname = work_list(ds)[0]["duckdb_file"]
                    S.build(ds, level_dir, "mix", spec["rows"], SEED, null_rate=spec["nulls"],
                            fresh=fname not in written, align="content", log=lambda *_: None)
                    written.add(fname)
                    print(f"== {level}: {ds} -> {fname} ({time.time() - t0:.0f}s)")
        print(f"== {level}: validating")
        report = validate(level_dir, args.benchmarks)
        manifest = {"level": level, "spec": spec, "seed": SEED, "align": "content", "built_by": "hp",
                    "date": time.strftime("%Y-%m-%d"), "benchmarks": report}
        old = level_dir / "manifest.json"
        if old.exists():
            prev = json.loads(old.read_text())
            prev["benchmarks"].update(report)
            manifest["benchmarks"] = prev["benchmarks"]
        old.write_text(json.dumps(manifest, indent=1))
        files = {}
        for base in manifest["benchmarks"]:
            for s in SETTINGS:
                files.setdefault(work_list(base + s)[0]["duckdb_file"], []).append(base + (s or "_documented"))
        (level_dir / "README.md").write_text(README.format(
            level=level, date=manifest["date"], rows=spec["rows"], nulls=spec["nulls"], seed=SEED,
            files="\n".join(f"| `{f}` | {', '.join(v)} |" for f, v in sorted(files.items())),
            example=next(iter(sorted(files)))))
        print(f"   wrote {old} and README.md")


if __name__ == "__main__":
    main()
