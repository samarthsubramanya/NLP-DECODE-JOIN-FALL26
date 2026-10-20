# DuckDB setup

```
pip install duckdb
python scripts/init_db.py      # creates data/decode_join.duckdb (gitignored)
```

Add data by creating a schema per database (`<dataset>__<db_id>`, e.g. `bird__financial`),
loading the tables into it, and registering it in `meta.databases` / `meta.columns` /
`meta.declared_fks` (see `db/schema.sql`).
