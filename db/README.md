# DuckDB setup

One file, `data/decode_join.duckdb` (gitignored; each person builds it locally).

```
pip install duckdb
python scripts/init_db.py                                   # creates meta.* tables
python scripts/load_sqlite.py --dataset bird --db-id financial path/to/financial.sqlite
python scripts/load_csv.py --dataset kaggledbqa --db-id mydb path/to/csv_dir
```

- Raw tables: one schema per database, `<dataset>__<db_id>` (e.g. `bird__financial`).
- Everything else is in `meta` (see `db/schema.sql`): original columns and declared keys,
  obfuscation map, gold questions/columns (from gold SQL, never used in profiling),
  Stage 1 profiles, Stage 2 PK/FK candidates, Stage 3 descriptions, a run log for every
  LLM call, and eval results.
- Settings (documented / stripped / obfuscated) are a column in the output tables, not
  separate copies of the data.
