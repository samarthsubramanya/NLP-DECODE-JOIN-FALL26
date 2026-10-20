# Shared DuckDB setup

## What this is

Our team shares one DuckDB database, `decode_join.duckdb`. It is empty right now: no tables,
no schema, no data. This setup only gives everyone the same starting file so people can start
loading datasets into it.

## How it works

DuckDB has no server. A DuckDB database is a single file on disk, so you cannot "connect to it"
over the network the way you would with Postgres. Instead:

- The **master copy** of the file lives in a private Hugging Face dataset repo:
  https://huggingface.co/datasets/SuhxsReddy/decode-join-duckdb
- Each person **pulls** the file to their own machine, works on it locally, then **pushes** it back.
- Git/GitHub does not hold the database file (it is gitignored). GitHub only holds the scripts.

```
        pull_db.py                         push_db.py
HF repo ----------> data/decode_join.duckdb ----------> HF repo
(master copy)       (your laptop)                       (master copy replaced)
```

## One-time setup (each person)

1. Ask Suhas to add your Hugging Face account as a collaborator on the repo above
   (needs write access).
2. Install the tools: `pip install duckdb huggingface_hub`
3. Log in: `hf auth login`

## Everyday use

```
python scripts/pull_db.py     # 1. download the latest master file to data/decode_join.duckdb
```

2. Work on it. In Python: `duckdb.connect("data/decode_join.duckdb")`, or in a terminal:
   `duckdb data/decode_join.duckdb`. Create tables, load data, run queries.

```
python scripts/push_db.py     # 3. upload your file, replacing the master copy
```

## The rule: one person at a time

Push **replaces** the master file. If two people change their copies at the same time, whoever
pushes last wipes out the other's work. So:

1. Pull right before you start.
2. Tell the group chat before you push ("pushing now").
3. Do not pull-and-edit while someone else is mid-push; pull again after they finish.

Keep sessions short: pull, make your change, push.

## Files in this branch (`database-setup`)

| File | What it does |
|---|---|
| `scripts/pull_db.py` | Downloads `decode_join.duckdb` from the HF repo into `data/` |
| `scripts/push_db.py` | Uploads `data/decode_join.duckdb` to the HF repo, overwriting it |
| `.gitignore` | Keeps the `.duckdb` file and the HF download cache out of git |
| `data/.gitkeep` | Keeps the `data/` folder in git |

## What was tested

The scripts were round-trip tested: pull, add a test table, push, delete the local file, pull
again (the table was there), then remove the table and push so the master is empty again.

## Limits and alternatives

- Not a live shared database. Only one writer at a time, by convention (nothing enforces it).
- The file is ~274 kB even when empty; that is normal for DuckDB.
- If we need many people connected at once, switch to MotherDuck (hosted DuckDB) instead.

## Not done yet

- No schema or data. Whoever loads the datasets decides the table layout.
- The `database-setup` branch has not been pushed to or merged on GitHub yet.

## Initial file in git

The empty starting database is committed once at `data/decode_join.duckdb` so the branch has a
ready-made starting file. The Hugging Face repo remains the master copy: after your first pull
this file will show as modified in git, and you should not commit it again. Use
`git update-index --assume-unchanged data/decode_join.duckdb` to hide that.
