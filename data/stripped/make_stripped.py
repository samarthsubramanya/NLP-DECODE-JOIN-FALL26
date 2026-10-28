"""Step 2: build the STRIPPED setting from the documented DuckDB file.

Stripped keeps: schema names, table names, column names, column order, physical types, rows.
Stripped removes: declared primary/foreign keys, UNIQUE/CHECK/NOT NULL constraints, indexes,
views, sequences, comments, and any metadata schema (BIRD's descriptions, dev.json, dev.sql).

How it works: every table is re-created with CREATE TABLE ... AS SELECT, which copies the
columns, their order, their types and the rows but none of the constraints, indexes or
comments. Schemas listed in --drop-schemas are not copied at all.

Usage:
    python3 make_stripped.py --src <documented>.duckdb \
        --dst <documented>_stripped.duckdb
    # quick dry run on one small database first:
    python3 make_stripped.py --src <documented>.duckdb \
        --dst /tmp/test_stripped.duckdb --only <schema_name>
"""
import argparse
import os
import time

import duckdb


def q(name):
    return '"' + name.replace('"', '""') + '"'


def lit(path):
    return "'" + path.replace("'", "''") + "'"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="documented .duckdb (read only)")
    ap.add_argument("--dst", required=True, help="stripped .duckdb to create")
    ap.add_argument("--drop-schemas", nargs="*", default=["bird_metadata"],
                    help="schemas that are NOT copied (descriptions, gold SQL, ...)")
    ap.add_argument("--only", nargs="*", default=None,
                    help="copy only these schemas (for a quick test)")
    ap.add_argument("--force", action="store_true", help="overwrite --dst if it exists")
    a = ap.parse_args()

    src, dst = os.path.abspath(a.src), os.path.abspath(a.dst)
    if not os.path.exists(src):
        raise SystemExit(f"source not found: {src}")
    if os.path.abspath(src) == os.path.abspath(dst):
        raise SystemExit("--dst must be a different file from --src")
    if os.path.exists(dst):
        if not a.force:
            raise SystemExit(f"{dst} exists; use --force to overwrite")
        os.remove(dst)

    con = duckdb.connect(dst)
    con.execute(f"ATTACH {lit(src)} AS src (READ_ONLY)")
    tables = con.execute(
        "SELECT schema_name, table_name FROM duckdb_tables() "
        "WHERE database_name = 'src' ORDER BY 1, 2").fetchall()

    drop = set(a.drop_schemas)
    keep = [(s, t) for s, t in tables
            if s not in drop and (a.only is None or s in a.only)]
    skipped = sorted({s for s, _ in tables if s in drop})
    print(f"{len(tables)} tables in source; copying {len(keep)}; "
          f"not copied (dropped schemas): {skipped or 'none'}")

    views = con.execute(
        "SELECT schema_name, view_name FROM duckdb_views() "
        "WHERE database_name = 'src' AND NOT internal").fetchall()
    if views:
        print(f"WARNING: source has {len(views)} views; they are NOT copied: {views[:5]}")

    t0 = time.time()
    made = set()
    for i, (s, t) in enumerate(keep, 1):
        if s not in made:
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {q(s)}")
            made.add(s)
        start = time.time()
        con.execute(f"CREATE TABLE {q(s)}.{q(t)} AS SELECT * FROM src.{q(s)}.{q(t)}")
        n = con.execute(f"SELECT COUNT(*) FROM {q(s)}.{q(t)}").fetchone()[0]
        print(f"[{i}/{len(keep)}] {s}.{t}: {n:,} rows ({time.time() - start:.1f}s)", flush=True)

    con.execute("DETACH src")
    con.execute("CHECKPOINT")
    con.close()
    print(f"\ndone in {time.time() - t0:.0f}s -> {dst} ({os.path.getsize(dst) / 1e6:,.0f} MB)")
    print("next: python3 verify_stripped.py --src <documented> --dst <stripped>")


if __name__ == "__main__":
    main()
