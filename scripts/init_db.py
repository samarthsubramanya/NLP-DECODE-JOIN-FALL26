"""Create the DECODE-JOIN DuckDB file and its metadata tables.

    python scripts/init_db.py [--db data/decode_join.duckdb]
"""
import argparse

from duckdb_utils import DEFAULT_DB, SCHEMA_SQL, connect


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    con = connect(args.db)
    con.execute(SCHEMA_SQL.read_text())
    n = con.execute("select count(*) from information_schema.tables where table_schema='meta'").fetchone()[0]
    print(f"{args.db}: {n} meta tables ready")


if __name__ == "__main__":
    main()
