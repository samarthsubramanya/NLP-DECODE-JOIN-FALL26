# DuckDB setup

One file, `data/decode_join.duckdb` (gitignored; each person builds it locally).

```
pip install -r requirements.txt
python scripts/init_db.py                                   # creates meta.* tables
```

- Raw tables: one schema per database, `<dataset>__<db_id>` (e.g. `bird__financial`).
- Everything else is in `meta` (see `db/schema.sql`): original columns and declared keys,
  obfuscation map, gold questions/columns (from gold SQL, never used in profiling),
  Stage 1 profiles, Stage 2 PK/FK candidates, Stage 3 descriptions, a run log for every
  LLM call, and eval results.
- Settings (documented / stripped / obfuscated) are a column in the output tables, not
  separate copies of the data.

Data loading is handled separately. Tests: `pytest`; CI runs them on every push (`.github/workflows/ci.yml`).
