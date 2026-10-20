"""Load a directory of CSV files (one per table) into DuckDB.

    python scripts/load_csv.py --dataset kaggledbqa --db-id mydb path/to/csv_dir
"""
import argparse
from pathlib import Path

from duckdb_utils import DEFAULT_DB, SCHEMA_SQL, connect, ident, schema_name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_dir")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--db-id", required=True)
    ap.add_argument("--db", default=DEFAULT_DB)
    a = ap.parse_args()

    con = connect(a.db)
    con.execute(SCHEMA_SQL.read_text())
    sch = schema_name(a.dataset, a.db_id)
    con.execute(f"drop schema if exists {ident(sch)} cascade")
    con.execute(f"create schema {ident(sch)}")
    con.execute("delete from meta.columns where dataset=? and db_id=?", [a.dataset, a.db_id])
    con.execute("delete from meta.databases where dataset=? and db_id=?", [a.dataset, a.db_id])
    for f in sorted(Path(a.csv_dir).glob("*.csv")):
        t = f.stem
        con.execute(f"create table {ident(sch)}.{ident(t)} as select * from read_csv_auto(?, sample_size=-1)", [str(f)])
        for i, (name, typ) in enumerate(con.execute(
                "select column_name, data_type from information_schema.columns "
                "where table_schema=? and table_name=? order by ordinal_position", [sch, t]).fetchall()):
            con.execute("insert into meta.columns values (?,?,?,?,?,?,NULL,FALSE)",
                        [a.dataset, a.db_id, t, name, i, typ])
        print(f"  {t}: {con.execute(f'select count(*) from {ident(sch)}.{ident(t)}').fetchone()[0]} rows")
    con.execute("insert into meta.databases (dataset, db_id, duck_schema, source_path) values (?,?,?,?)",
                [a.dataset, a.db_id, sch, a.csv_dir])
    print(f"loaded {a.dataset}/{a.db_id} into schema {sch}")


if __name__ == "__main__":
    main()
