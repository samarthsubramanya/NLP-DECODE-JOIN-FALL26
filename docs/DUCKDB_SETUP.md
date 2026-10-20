# Shared DuckDB setup

## What this is

Our team shares one DuckDB database, `decode_join.duckdb`. It is empty right now: no tables,
no schema, no data. This setup only gives everyone the same starting file so people can start
loading datasets into it.

## How it works

DuckDB has no server. A DuckDB database is a single file on disk, so you cannot "connect to it"
over the network the way you would with Postgres. Instead:

- The **master copy** of the file lives in a Hugging Face dataset repo (public, gated):
  https://huggingface.co/datasets/SuhxsReddy/decode-join-duckdb
- Each person **pulls** the file to their own machine, works on it locally, then **pushes** it back.
- Git/GitHub does not hold the database file (it is gitignored). GitHub only holds the scripts.

```
        pull_db.py                         push_db.py
HF repo ----------> data/decode_join.duckdb ----------> HF repo
(master copy)       (your laptop)                       (pull request, owner merges)
```

## One-time setup (each person)

1. Install the tools: `pip install duckdb huggingface_hub`
2. Log in: `hf auth login`
3. Open the repo link above once while logged in and click "Agree and access repository"
   (the repo is public but gated, so this is required before you can download).

You do **not** need write access. Only Suhas (the repo owner) can write to the master copy;
everyone else proposes changes with a pull request.

## Everyday use

```
python scripts/pull_db.py                    # 1. download the latest master file
```

2. Work on it. In Python: `duckdb.connect("data/decode_join.duckdb")`, or in a terminal:
   `duckdb data/decode_join.duckdb`. Create tables, load data, run queries.

```
python scripts/push_db.py "what I changed"   # 3. open a pull request with your file
```

This prints a link to the pull request. Tell Suhas in the group chat.

**Suhas (owner) merges:** open the repo -> Community tab -> the pull request -> Merge.
(The owner can also overwrite directly with `python scripts/push_db.py --direct "message"`.)

## The rule: one change at a time

A DuckDB file cannot be merged automatically. Each pull request contains a whole file, so if
two people both change their copy from the same starting point, merging one makes the other
out of date. So:

1. Pull right before you start, and keep your session short.
2. Tell the group chat when you open a pull request.
3. If someone else's pull request was merged first, pull again and redo your change on top.
   Do not push an out-of-date file.

## Files in this branch (`database-setup`)

| File | What it does |
|---|---|
| `scripts/pull_db.py` | Downloads `decode_join.duckdb` from the HF repo into `data/` |
| `scripts/push_db.py` | Opens a pull request on the HF repo with your `data/decode_join.duckdb` (owner can use `--direct`) |
| `.gitignore` | Keeps the `.duckdb` file and the HF download cache out of git |
| `data/.gitkeep` | Keeps the `data/` folder in git |

## What was tested

Pull and push were round-trip tested (add a table, push, pull on a clean folder, table present).
The pull request flow was tested by opening a pull request and closing it; the master is unchanged and empty.

## Limits and alternatives

- Not a live shared database. Changes go in as whole-file pull requests that the owner merges one at a time.
- Everyone downloads the whole file on each pull. Fine while it is small; if the five datasets make it several GB, move to MotherDuck or Parquet files.
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
