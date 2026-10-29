"""Step 3: prove the stripped file differs from the documented one ONLY in metadata.

Checks (all must pass):
  1. same schemas and tables, except the dropped metadata schemas
  2. same column names, order and types in every table
  3. same row count in every table
  4. same per-column checksum (an order-independent hash of every value, plus the
     non-null count) in every table, so the data is identical, not just the shape
  5. the stripped file carries no constraints, indexes, views, sequences or comments
  6. none of the dropped schemas exist in the stripped file

Usage:
    python3 verify_stripped.py --src <documented>.duckdb \
        --dst <documented>_stripped.duckdb
    python3 verify_stripped.py ... --only bird_<schema>      # for a test build
Exit status 0 = all passed, 1 = something failed.
"""
import argparse
import os
import sys

import duckdb


def q(name):
    return '"' + name.replace('"', '""') + '"'


def lit(path):
    return "'" + path.replace("'", "''") + "'"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--drop-schemas", nargs="*", default=["bird_metadata"])
    ap.add_argument("--only", nargs="*", default=None)
    a = ap.parse_args()

    con = duckdb.connect()
    con.execute(f"ATTACH {lit(os.path.abspath(a.src))} AS o (READ_ONLY)")
    con.execute(f"ATTACH {lit(os.path.abspath(a.dst))} AS s (READ_ONLY)")
    drop = set(a.drop_schemas)
    failures = []

    def check(ok, label, detail=""):
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    def tables_of(db):
        return {(sc, t) for sc, t in con.execute(
            "SELECT schema_name, table_name FROM duckdb_tables() WHERE database_name = ?", [db]).fetchall()}

    expected = {(sc, t) for sc, t in tables_of("o")
                if sc not in drop and (a.only is None or sc in a.only)}
    actual = tables_of("s")
    check(actual == expected, "same schemas and tables (minus dropped schemas)",
          f"expected {len(expected)}, found {len(actual)}"
          + (f"; missing {sorted(expected - actual)[:3]}" if expected - actual else "")
          + (f"; extra {sorted(actual - expected)[:3]}" if actual - expected else ""))

    def columns_of(db, sc, t):
        return con.execute(
            "SELECT column_name, data_type FROM duckdb_columns() WHERE database_name = ? "
            "AND schema_name = ? AND table_name = ? ORDER BY column_index", [db, sc, t]).fetchall()

    bad_cols, bad_rows, bad_sum = [], [], []
    for sc, t in sorted(expected & actual):
        co, cs = columns_of("o", sc, t), columns_of("s", sc, t)
        if co != cs:
            bad_cols.append(f"{sc}.{t}")
            continue
        parts = ["COUNT(*)"]
        for name, _ in co:
            parts.append(f"COUNT({q(name)})")
            parts.append(f"SUM(CAST(hash({q(name)}) AS HUGEINT))")
        ro = con.execute(f"SELECT {', '.join(parts)} FROM o.{q(sc)}.{q(t)}").fetchone()
        rs = con.execute(f"SELECT {', '.join(parts)} FROM s.{q(sc)}.{q(t)}").fetchone()
        if ro[0] != rs[0]:
            bad_rows.append(f"{sc}.{t} ({ro[0]} vs {rs[0]})")
        elif ro != rs:
            bad_sum.append(f"{sc}.{t}")
        print(f"   checked {sc}.{t}: {ro[0]:,} rows, {len(co)} columns", flush=True)

    check(not bad_cols, "same column names, order and types", str(bad_cols[:3]) if bad_cols else "")
    check(not bad_rows, "same row counts", str(bad_rows[:3]) if bad_rows else "")
    check(not bad_sum, "same per-column checksums", str(bad_sum[:3]) if bad_sum else "")

    for label, sql in (
        ("no constraints (PK, FK, UNIQUE, CHECK, NOT NULL)",
         "SELECT COUNT(*) FROM duckdb_constraints() WHERE database_name = 's'"),
        ("no indexes", "SELECT COUNT(*) FROM duckdb_indexes() WHERE database_name = 's'"),
        ("no views", "SELECT COUNT(*) FROM duckdb_views() WHERE database_name = 's' AND NOT internal"),
        ("no sequences", "SELECT COUNT(*) FROM duckdb_sequences() WHERE database_name = 's'"),
        ("no column comments",
         "SELECT COUNT(*) FROM duckdb_columns() WHERE database_name = 's' AND comment IS NOT NULL"),
        ("no table comments",
         "SELECT COUNT(*) FROM duckdb_tables() WHERE database_name = 's' AND comment IS NOT NULL"),
    ):
        n = con.execute(sql).fetchone()[0]
        check(n == 0, label, f"found {n}" if n else "")

    schemas_s = {r[0] for r in con.execute(
        "SELECT schema_name FROM duckdb_schemas() WHERE database_name = 's'").fetchall()}
    leaked = sorted(drop & schemas_s)
    check(not leaked, "dropped metadata schemas are absent", str(leaked) if leaked else "")

    print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILED: ' + '; '.join(failures)}")
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
