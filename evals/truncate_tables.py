"""Copies benchmark DuckDB files with every table cut independently to a share of its rows.

Recreates what BEAVER dw looks like (tables capped separately, so parents lose rows their children reference) on
other benchmarks. Point the pipeline at the copies with DECODE_JOIN_DUCKDB_DIR. <dataset>_metadata schemas are copied
whole, so grading still works. Row choice depends on the table and seed only, so obfuscated and named copies match
when their rows are stored in the same order. A table always keeps at least one row.

Usage: python evals/truncate_tables.py <out_dir> --keep 0.5 --seed 0 --datasets bird bird_obfuscated
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT.parent / "data" / "decode-join-duckdb"


def q(name):
    return '"' + name.replace('"', '""') + '"'


def truncate(src, dst, schemas, keep, seed):
    """schemas: {schema: [table, ...]}; every table of a *_metadata schema is copied as it is."""
    con = duckdb.connect(str(dst))
    con.execute(f"ATTACH '{src}' AS src (READ_ONLY)")
    for schema, tables in schemas.items():
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {q(schema)}")
        for t in tables:
            source, name = f"src.{q(schema)}.{q(t)}", f"{q(schema)}.{q(t)}"
            con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * EXCLUDE (_k) FROM ("
                        f"SELECT *, hash(concat(rowid, '|', ?, '|', ?)) AS _k FROM {source}) "
                        f"QUALIFY row_number() OVER (ORDER BY _k) <= ceil(? * count(*) OVER ())", [t, seed, keep])
    for (schema,) in con.execute("SELECT DISTINCT table_schema FROM information_schema.tables "
                                 "WHERE table_catalog = 'src' AND table_schema LIKE '%\\_metadata' ESCAPE '\\'").fetchall():
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {q(schema)}")
        for (t,) in con.execute("SELECT table_name FROM information_schema.tables WHERE table_catalog = 'src' "
                                "AND table_schema = ?", [schema]).fetchall():
            con.execute(f"CREATE OR REPLACE TABLE {q(schema)}.{q(t)} AS SELECT * FROM src.{q(schema)}.{q(t)}")
    con.close()


def _selfcheck(tmp):
    src, dst = tmp / "a.duckdb", tmp / "b.duckdb"
    c = duckdb.connect(str(src))
    c.execute("CREATE SCHEMA s; CREATE TABLE s.big AS SELECT i FROM range(100) t(i); CREATE TABLE s.one AS SELECT 1 AS i;"
              "CREATE SCHEMA x_metadata; CREATE TABLE x_metadata.m AS SELECT i FROM range(7) t(i)")
    c.close()
    truncate(src, dst, {"s": ["big", "one"]}, 0.3, 0)
    c = duckdb.connect(str(dst))
    n = [c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("s.big", "s.one", "x_metadata.m")]
    c.close()
    src.unlink(), dst.unlink()
    assert n == [30, 1, 7], n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--keep", type=float, required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--datasets", nargs="+", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    _selfcheck(out)
    files = defaultdict(lambda: defaultdict(list))
    for d in args.datasets:
        for db in json.loads((ROOT / f"out/filtered/{d}/databases.json").read_text()):
            files[db["duckdb_file"]][db["duckdb_schema"]] = db["tables"]
    for f, schemas in files.items():
        truncate(SOURCE / f, out / f, schemas, args.keep, args.seed)
        print("ok", f, len(schemas), "schemas")


if __name__ == "__main__":
    main()
