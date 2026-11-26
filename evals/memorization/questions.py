"""Memorization probe, step 1 (hp): new questions over tables that no gold question uses.

A model that memorized BIRD or Spider can name the right tables for a benchmark question without reading the
schema. It cannot do that for a question nobody wrote before about tables no benchmark question touches. These
questions are the control set for probe.py.

    python evals/memorization/questions.py unused --datasets bird spider fiben beaver kaggledbqa
    python evals/memorization/questions.py write  --datasets bird spider --per-db 5

unused  per database: tables minus the union of gold tables (out/filtered/<benchmark>/databases.json), with row
        counts from the documented file. Writes out/memorization/<benchmark>/unused.json.
write   the LLM (Voyager, same client and cache as Stage 3) drafts 2 x --per-db questions with DuckDB SQL per
        database, using only unused tables with rows; each draft is kept only if
          - sqlglot parses it and every table it reads is an unused table of that database,
          - it runs on the documented file and returns at least one row,
        then up to --per-db are kept, multi-table ones first. Gold tables and columns come from the SQL itself.
        Writes evals/memorization/questions/<benchmark>.jsonl with "reviewed": false. Read every question before
        using it (does the SQL answer the question? is it natural?) and set "reviewed": true or delete the line.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "evals"), str(ROOT / "evals" / "sparsity")]
from common import source_file, work_list   # noqa: E402

OUT = ROOT / "out" / "memorization"
QDIR = Path(__file__).resolve().parent / "questions"

SYSTEM = """You write evaluation questions for a text-to-SQL benchmark.
You get some tables of one database (names, column types, a few sample rows). Write questions a real analyst
might ask about THESE tables only, each with one DuckDB SQL query that answers it.
Rules:
- Use only the tables given. Prefer questions that need a JOIN of two or more of them when they can be joined.
- Questions in plain English, without table or column names in them; refer to things the way a user would.
- The SQL must run on DuckDB, with table and column names quoted exactly as given, no schema prefix.
- Vary the questions: lookups, counts, aggregates, comparisons, filters.
Return JSON only."""


def q(name):
    return '"' + name.replace('"', '""') + '"'


def connect(db):
    import duckdb
    return duckdb.connect(str(source_file(db["duckdb_file"])), read_only=True)


def unused_tables(base):
    out = {}
    dbs = work_list(base)
    con = connect(dbs[0])
    for db in dbs:
        used = {t.lower() for t in db.get("gold_tables") or []}
        tables = {}
        for t in db["tables"]:
            if t.lower() in used:
                continue
            rows = con.execute(f"SELECT count(*) FROM {q(db['duckdb_schema'])}.{q(t)}").fetchone()[0]
            tables[t] = rows
        out[db["db_id"]] = {"duckdb_schema": db["duckdb_schema"], "all_tables": len(db["tables"]),
                            "gold_tables": len(used), "unused": tables}
    con.close()
    return out


def cmd_unused(args):
    for base in args.datasets:
        res = unused_tables(base)
        path = OUT / base / "unused.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(res, indent=1))
        n = sum(len(v["unused"]) for v in res.values())
        rows = sum(1 for v in res.values() for r in v["unused"].values() if r)
        print(f"{base}: {n} unused tables ({rows} with rows) in {sum(bool(v['unused']) for v in res.values())}"
              f"/{len(res)} databases")
        for db, v in res.items():
            print(f"  {db}: {len(v['unused'])}/{v['all_tables']} unused: {', '.join(sorted(v['unused']))[:150]}")


def table_payload(con, schema, table, n_rows=3):
    cols = con.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = ? "
                       "AND table_name = ? ORDER BY ordinal_position", [schema, table]).fetchall()
    rows = con.execute(f"SELECT * FROM {q(schema)}.{q(table)} USING SAMPLE {n_rows} ROWS (reservoir, 42)").fetchall()
    return {"columns": {c: t for c, t in cols},
            "sample_rows": [[None if v is None else str(v)[:60] for v in r] for r in rows]}


def sql_tables_columns(sql, schema_cols):
    """(tables, ["table.column"]) the SQL reads, by sqlglot qualify; None if it does not parse."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.qualify import qualify
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return None
    tables = sorted({t.name for t in tree.find_all(exp.Table) if t.name})
    cols = set()
    try:
        qt = qualify(tree.copy(), schema=schema_cols, dialect="duckdb", validate_qualify_columns=False,
                     quote_identifiers=False)
        alias = {}
        for t in qt.find_all(exp.Table):
            alias[(t.alias_or_name or t.name)] = t.name
        for c in qt.find_all(exp.Column):
            if c.table in alias and c.name:
                cols.add(f"{alias[c.table]}.{c.name}")
    except Exception:
        pass
    return tables, sorted(cols)


def schema_for_sqlglot(con, schema, tables):
    out = {}
    for t in tables:
        out[t] = {c: dt for c, dt in con.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = ? AND table_name = ?",
            [schema, t]).fetchall()}
    return out


def validate(con, db, draft, unused, schema_cols):
    sql = (draft.get("sql") or "").strip().rstrip(";")
    if not sql or not (draft.get("question") or "").strip():
        return None, "empty"
    parsed = sql_tables_columns(sql, schema_cols)
    if parsed is None:
        return None, "parse"
    tables, cols = parsed
    lower = {t.lower(): t for t in unused}
    if not tables or any(t.lower() not in lower for t in tables):
        return None, "uses a gold or unknown table"
    try:
        con.execute(f"SET search_path = '{db['duckdb_schema']}'")
        rows = con.execute(f"SELECT * FROM ({sql}) LIMIT 5").fetchall()
    except Exception as e:   # noqa: BLE001
        return None, f"sql error: {str(e)[:80]}"
    if not rows:
        return None, "no rows"
    return {"question": draft["question"].strip(), "gold_sql": sql, "gold_tables": [lower[t.lower()] for t in tables],
            "gold_columns": cols, "num_gold_tables": len(tables)}, "ok"


SCHEMA = {"name": "new_questions", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["questions"],
    "properties": {"questions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["question", "sql"],
        "properties": {"question": {"type": "string"}, "sql": {"type": "string"}}}}}}}


def cmd_write(args):
    from describe_columns import DEFAULTS as LLM, Voyager
    cfg = {**LLM, "reasoning_effort": args.reasoning_effort, "model": args.model}
    for base in args.datasets:
        unused_path = OUT / base / "unused.json"
        if not unused_path.exists():
            sys.exit(f"run `questions.py unused --datasets {base}` first")
        unused = json.loads(unused_path.read_text())
        dbs = {d["db_id"]: d for d in work_list(base)}
        con = connect(next(iter(dbs.values())))
        llm = Voyager(cfg, (OUT / base / "llm_calls.jsonl"))
        out_path = QDIR / f"{base}.jsonl"
        QDIR.mkdir(parents=True, exist_ok=True)
        if out_path.exists() and not args.force:
            sys.exit(f"{out_path.relative_to(ROOT)} exists (reviewed questions live there); --force to overwrite")
        kept_all, reasons = [], {}
        for db_id, info in unused.items():
            tables = [t for t, rows in info["unused"].items() if rows]
            if not tables:
                continue
            db = dbs[db_id]
            payload = {"database": db_id, "n_questions": 2 * args.per_db,
                       "tables": {t: table_payload(con, db["duckdb_schema"], t) for t in tables[:args.max_tables]}}
            reply, _ = llm.complete("new_questions", db_id, SYSTEM, json.dumps(payload, default=str), SCHEMA)
            schema_cols = schema_for_sqlglot(con, db["duckdb_schema"], tables)
            good = []
            for d in (reply or {}).get("questions", []):
                v, why = validate(con, db, d, tables, schema_cols)
                reasons[why] = reasons.get(why, 0) + 1
                if v:
                    good.append(v)
            good.sort(key=lambda v: -v["num_gold_tables"])
            for i, v in enumerate(good[:args.per_db]):
                kept_all.append({"uid": f"{base}:new:{db_id}:{i}", "dataset": base, "db_id": db_id,
                                 "duckdb_file": db["duckdb_file"], "duckdb_schema": db["duckdb_schema"], **v,
                                 "source": "llm-authored over unused tables", "model": cfg["model"], "reviewed": False})
            print(f"  {db_id}: {len(good)} valid of {len((reply or {}).get('questions', []))}, kept {min(len(good), args.per_db)}")
        out_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept_all))
        print(f"{base}: {len(kept_all)} questions -> {out_path.relative_to(ROOT)}; drafts by outcome {reasons}")
        con.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("unused")
    u.add_argument("--datasets", nargs="+", required=True)
    w = sub.add_parser("write")
    w.add_argument("--datasets", nargs="+", required=True)
    w.add_argument("--per-db", type=int, default=5)
    w.add_argument("--max-tables", type=int, default=12, help="at most this many unused tables shown per database")
    w.add_argument("--model", default="glm-5-3")
    w.add_argument("--reasoning-effort", default="low", choices=["none", "low", "medium", "high"])
    w.add_argument("--force", action="store_true")
    args = ap.parse_args()
    {"unused": cmd_unused, "write": cmd_write}[args.cmd](args)


if __name__ == "__main__":
    main()
