"""Load a SQLite database (Spider, BIRD, KaggleDBQA, ... ship as .sqlite) into DuckDB.

Raw tables go to schema <dataset>__<db_id>; the original column list and declared
PK/FKs are recorded in meta.* so the stripped/obfuscated settings can be derived.

    python scripts/load_sqlite.py --dataset bird --db-id financial path/to/financial.sqlite
"""
import argparse
import sqlite3

from duckdb_utils import DEFAULT_DB, SCHEMA_SQL, connect, ident, schema_name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sqlite_path")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--db-id", required=True)
    ap.add_argument("--db", default=DEFAULT_DB)
    a = ap.parse_args()

    con = connect(a.db)
    con.execute(SCHEMA_SQL.read_text())
    sch = schema_name(a.dataset, a.db_id)
    con.execute(f"drop schema if exists {ident(sch)} cascade")
    con.execute(f"create schema {ident(sch)}")
    for t in ("columns", "declared_fks"):
        con.execute(f"delete from meta.{t} where dataset=? and db_id=?", [a.dataset, a.db_id])
    con.execute("delete from meta.databases where dataset=? and db_id=?", [a.dataset, a.db_id])

    lite = sqlite3.connect(a.sqlite_path)
    lite.text_factory = lambda b: b.decode("utf-8", errors="replace")
    tables = [r[0] for r in lite.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    for t in tables:
        info = lite.execute(f"pragma table_info({ident(t)})").fetchall()
        cols = [(c[1], (c[2] or "VARCHAR").upper(), c[5] > 0) for c in info]
        # SQLite types are loose; DuckDB maps them, falling back to VARCHAR.
        defs = []
        for name, typ, _ in cols:
            try:
                con.execute(f"select cast(null as {typ})")
            except Exception:
                typ = "VARCHAR"
            defs.append(f"{ident(name)} {typ}")
        con.execute(f"create table {ident(sch)}.{ident(t)} ({', '.join(defs)})")
        rows = lite.execute(f"select * from {ident(t)}").fetchall()
        if rows:
            ph = ",".join("?" * len(cols))
            con.executemany(f"insert into {ident(sch)}.{ident(t)} values ({ph})", rows)
        for i, (name, typ, pk) in enumerate(cols):
            con.execute("insert into meta.columns values (?,?,?,?,?,?,NULL,?)",
                        [a.dataset, a.db_id, t, name, i, typ, pk])
        for fk in lite.execute(f"pragma foreign_key_list({ident(t)})"):
            con.execute("insert into meta.declared_fks values (?,?,?,?,?,?)",
                        [a.dataset, a.db_id, t, fk[3], fk[2], fk[4]])
        print(f"  {t}: {len(rows)} rows")
    con.execute("insert into meta.databases (dataset, db_id, duck_schema, source_path) values (?,?,?,?)",
                [a.dataset, a.db_id, sch, a.sqlite_path])
    print(f"loaded {a.dataset}/{a.db_id} into schema {sch}")


if __name__ == "__main__":
    main()
