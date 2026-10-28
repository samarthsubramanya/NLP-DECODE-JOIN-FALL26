"""
Import the KaggleDBQA SQLite databases into one DuckDB file (one schema per SQLite file).

Usage:
    python import_kaggledbqa.py --kaggle-dir path/to/KaggleDBQA --duckdb decode_join_kaggledbqa.duckdb

Outputs:
    - the .duckdb file
    - schema_mapping.csv (source_path, duckdb_schema) in the current folder
"""
import argparse
import csv
import hashlib
from pathlib import Path

import duckdb


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kaggle-dir", required=True)
    ap.add_argument("--duckdb", required=True)
    ap.add_argument("--mapping-out", default="schema_mapping.csv")
    a = ap.parse_args()

    root = Path(a.kaggle_dir).expanduser()
    db_path = Path(a.duckdb).expanduser()
    if db_path.exists():
        db_path.unlink()

    con = duckdb.connect(str(db_path))
    con.execute("INSTALL sqlite; LOAD sqlite;")
    rows = []
    for f in sorted(root.glob("databases/*/*.sqlite")):
        rel = f.relative_to(root).as_posix()
        schema = "kaggledbqa_" + hashlib.sha256(rel.encode()).hexdigest()[:20]
        con.execute(f"CREATE SCHEMA {quote(schema)}")
        con.execute(f"ATTACH '{f}' AS src (TYPE sqlite)")
        tables = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_catalog='src' AND table_schema='main'"
        ).fetchall()
        for (t,) in tables:
            target = f"{quote(schema)}.{quote(t)}"
            try:
                con.execute(f"CREATE TABLE {target} AS SELECT * FROM src.{quote(t)}")
            except Exception:
                print(f"  text fallback: {rel} / {t}")
                con.execute("SET sqlite_all_varchar=true")
                con.execute(f"DROP TABLE IF EXISTS {target}")
                con.execute(f"CREATE TABLE {target} AS SELECT * FROM src.{quote(t)}")
                con.execute("SET sqlite_all_varchar=false")
            n = con.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0]
            print(f"{rel} / {t}: {n} rows")
        con.execute("DETACH src")
        rows.append((rel, schema))
    con.close()

    with open(a.mapping_out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["source_path", "duckdb_schema"])
        w.writerows(rows)
    print(f"Done: {len(rows)} databases -> {db_path}")


if __name__ == "__main__":
    main()
