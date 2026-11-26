"""Sparse copies of a benchmark's DuckDB file: the data the pipeline sees gets thinner, nothing else changes.

Writes <out_dir>/<duckdb_file> with only this dataset's schemas (and every *_metadata schema copied whole, so grading
and later stages still work with DECODE_JOIN_DUCKDB_DIR=<out_dir>). The original file is attached read-only.

Modes (rate = how much is taken away, 0 = an exact copy):
    row_sample     every table keeps (1 - rate) of its rows, chosen independently per table   [no gold needed]
    cell_null      every cell of every column is nulled with probability rate                 [no gold needed]
    hard           row_sample and cell_null together at the same rate (the hardest setting)   [no gold needed]
    fk_null        cells of gold FK *source* columns are nulled with probability rate         [gold chooses columns]
    pk_null        cells of gold FK *target* (key) columns are nulled with probability rate   [gold chooses columns]
    parent_delete  gold FK *target* tables keep (1 - rate) of their rows                       [gold chooses tables]

Gold only decides WHICH columns or tables are changed; the pipeline never reads it. Gold FK direction: BIRD and
Spider declare it; for FIBEN, BEAVER and KaggleDBQA the end that is unique and null-free in the data is the target
(both: the table with more rows, a subclass points at its superclass; neither: the pair is skipped).

Row and cell choices depend only on (table, column, rowid, seed), so stripped and obfuscated copies of the same
benchmark lose the same rows and cells. A table always keeps at least one row (if it had any).

    python evals/sparsity/sparsify.py /tmp/sparse/bird_stripped --dataset bird_stripped --mode hard --rate 0.5 --seed 0
"""
import argparse
import json
from pathlib import Path

import common  # noqa: F401  (sets sys.path)
from common import Gold, source_file, work_list

MODES = ("row_sample", "cell_null", "hard", "fk_null", "pk_null", "parent_delete")
GOLD_MODES = ("fk_null", "pk_null", "parent_delete")
SCALE = 1_000_000


def q(name):
    return '"' + name.replace('"', '""') + '"'


def lit(text):
    return "'" + str(text).replace("'", "''") + "'"


def table_columns(con, catalog, schema, table):
    return [c for (c,) in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_catalog = ? AND table_schema = ? "
        "AND table_name = ? ORDER BY ordinal_position", [catalog, schema, table]).fetchall()]


def is_key(con, catalog, schema, table, column):
    n, d, nn = con.execute(f"SELECT count(*), count(DISTINCT {q(column)}), count({q(column)}) "
                           f"FROM {catalog}.{q(schema)}.{q(table)}").fetchone()
    return n > 0 and n == d == nn


def row_count(con, catalog, schema, table):
    return con.execute(f"SELECT count(*) FROM {catalog}.{q(schema)}.{q(table)}").fetchone()[0]


def directed_gold(con, gold, db, schema, actual):
    """[(source (table, col), target (table, col))] in this variant's actual names. actual: {lower table: (name, {lower col: name})}."""
    out = []
    for pair in sorted(gold.pairs(db), key=sorted):
        ends = []
        for t, c in sorted(pair):
            st, sc = gold.shown(db, t, c)
            if st.lower() not in actual or sc.lower() not in actual[st.lower()][1]:
                break
            tname, cols = actual[st.lower()]
            ends.append((tname, cols[sc.lower()]))
        if len(ends) != 2:
            continue
        declared = getattr(gold, "directed", {}).get(db)
        fk = next((d for d in declared or () if frozenset(d) == pair), None)
        if fk is not None:   # BIRD / Spider: the declared direction; ends follow sorted(pair)
            if fk[0] != sorted(pair)[0]:
                ends.reverse()
            out.append(tuple(ends))
            continue
        keys = [is_key(con, "src", schema, t, c) for t, c in ends]
        if keys == [True, False]:
            ends.reverse()
        elif keys == [True, True]:
            if row_count(con, "src", schema, ends[0][0]) > row_count(con, "src", schema, ends[1][0]):
                ends.reverse()
        elif keys == [False, False]:
            continue
        out.append(tuple(ends))
    return out


def declared_direction(gold):
    """BIRD / Spider: {db: [((t, c), (t, c)) source -> target]} from the declared FKs, real lower-case names."""
    if gold.base not in ("bird", "spider"):
        return {}
    from duckdb_driver import open_db
    dbs = json.loads((common.ROOT / f"out/filtered/{gold.base}/databases.json").read_text())
    con = open_db(dbs[0]["duckdb_file"])
    return {db: sorted(gold.labels.gold(con, f"{gold.base}_metadata", schema)[1]) for db, schema in gold.schemas.items()}


def plan_for(con, db, mode, rate, gold):
    """{table: {"keep": share or None, "null": {column: rate}}} for one database of the work list."""
    schema, tables = db["duckdb_schema"], db["tables"]
    actual = {t.lower(): (t, {c.lower(): c for c in table_columns(con, "src", schema, t)}) for t in tables}
    plan = {t: {"keep": None, "null": {}} for t in tables}
    if mode in ("row_sample", "hard"):
        for t in tables:
            plan[t]["keep"] = 1 - rate
    if mode in ("cell_null", "hard"):
        for t in tables:
            plan[t]["null"] = {c: rate for c in actual[t.lower()][1].values()}
    if mode in GOLD_MODES:
        edges = directed_gold(con, gold, db["db_id"], schema, actual)
        for (st, sc), (tt, tc) in edges:
            if mode == "fk_null":
                plan[st]["null"][sc] = rate
            elif mode == "pk_null":
                plan[tt]["null"][tc] = rate
            else:
                plan[tt]["keep"] = 1 - rate
        plan["_gold_edges"] = [[list(s), list(t)] for s, t in edges]
    return plan


def copy_table(con, schema, table, columns, spec, seed):
    """CREATE dst table from src with spec's row share and per-column null rates; deterministic in (table, seed)."""
    row_key = f"hash(concat(rowid, '|', {lit(table)}, '|', {lit(seed)}))"
    exprs = []
    for c in columns:
        r = spec["null"].get(c, 0)
        if r > 0:
            cell = f"hash(concat(_k, '|', {lit(c)})) % {SCALE}"
            exprs.append(f"CASE WHEN {cell} < {int(round(r * SCALE))} THEN NULL ELSE {q(c)} END AS {q(c)}")
        else:
            exprs.append(q(c))
    keyed = f"SELECT *, {row_key} AS _k FROM src.{q(schema)}.{q(table)}"
    keep = spec["keep"]
    if keep is not None and keep < 1:   # same pattern as evals/truncate_tables.py
        keyed = (f"SELECT * FROM ({keyed}) QUALIFY row_number() OVER (ORDER BY _k) "
                 f"<= greatest(1, ceil({float(keep)!r} * count(*) OVER ()))")
    # rows are chosen on the row key first, so a row's survival does not depend on which cells are nulled
    con.execute(f"CREATE OR REPLACE TABLE {q(schema)}.{q(table)} AS SELECT {', '.join(exprs)} FROM ({keyed})")


def build(dataset, out_dir, mode, rate, seed, databases=None, log=print):
    """Writes the sparse copy; returns {"file": path, "plans": {db: plan}}. rate 0 gives an exact copy."""
    import duckdb
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    dbs = work_list(dataset, databases)
    files = {d["duckdb_file"] for d in dbs}
    if len(files) != 1:
        raise ValueError(f"{dataset}: expected one DuckDB file, got {files}")
    filename = files.pop()
    src = source_file(filename)
    dst = Path(out_dir) / filename
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    gold = None
    if mode in GOLD_MODES and rate > 0:
        gold = Gold(dataset)
        gold.directed = declared_direction(gold)
    con = duckdb.connect(str(dst))
    con.execute(f"ATTACH {lit(src)} AS src (READ_ONLY)")
    plans = {}
    for db in dbs:
        schema = db["duckdb_schema"]
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {q(schema)}")
        plan = plan_for(con, db, mode, rate, gold) if rate > 0 else {t: {"keep": None, "null": {}} for t in db["tables"]}
        for t in db["tables"]:
            copy_table(con, schema, t, table_columns(con, "src", schema, t), plan[t], seed)
        plans[db["db_id"]] = plan
        if "_gold_edges" in plan:
            log(f"  {db['db_id']}: {mode} {rate} on {len(plan['_gold_edges'])} gold FKs")
    for (meta,) in con.execute("SELECT DISTINCT table_schema FROM information_schema.tables WHERE table_catalog = 'src' "
                               "AND table_schema LIKE '%\\_metadata' ESCAPE '\\'").fetchall():
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {q(meta)}")
        for (t,) in con.execute("SELECT table_name FROM information_schema.tables WHERE table_catalog = 'src' "
                                "AND table_schema = ?", [meta]).fetchall():
            con.execute(f"CREATE OR REPLACE TABLE {q(meta)}.{q(t)} AS SELECT * FROM src.{q(meta)}.{q(t)}")
    con.execute("DETACH src")
    con.close()
    return {"file": dst, "plans": plans}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--mode", required=True, choices=MODES)
    ap.add_argument("--rate", type=float, required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--databases", nargs="*")
    args = ap.parse_args()
    res = build(args.dataset, args.out_dir, args.mode, args.rate, args.seed, args.databases)
    print(json.dumps({"file": str(res["file"]), "dataset": args.dataset, "mode": args.mode, "rate": args.rate,
                      "seed": args.seed}))


if __name__ == "__main__":
    main()
