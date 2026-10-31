"""Grade a FIBEN run of Stages 2a, 2b and 3.

Labels, read only here:
    primary keys  PRIMARY KEY constraints in the FIBEN DuckDB file (duckdb_constraints(); 150 tables)
    foreign keys  not in the DuckDB file: data/fiben/import_fiben.py skips FIBEN.sql's ALTER TABLE
                  statements, so they are read from data/fiben/FIBEN.sql (IBM/fiben-benchmark; 159 FKs,
                  135 of them primary key -> primary key subclass links). --fiben-sql overrides the path.
    query joins   column pairs the gold SQL joins on, from out/filtered/fiben/questions.jsonl (recall only)
FIBEN ships no column descriptions, so Stage 3 descriptions are only counted by status.

Usage (from the repo root):
    python evals/grade_fiben.py [--keys-run frozen] [--joins-run default] [--stage3-run low]

Output: out/descriptions/fiben/<stage3-run>/grade.json when --stage3-run is given, and a printed summary.
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))
from duckdb_driver import open_db       # noqa: E402
from grade_beaver import prf, query_joins   # noqa: E402

ALTER_FK = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+(?:CONSTRAINT\s+\w+\s+)?FOREIGN\s+KEY\s*\(([^)]*)\)\s*"
                      r"REFERENCES\s+(\w+)\s*\(([^)]*)\)", re.I)


def sql_fks(path):
    out = set()
    for m in ALTER_FK.finditer(Path(path).read_text()):
        for a, b in zip(m[2].split(","), m[4].split(",")):
            out.add(frozenset(((m[1].lower(), a.strip().lower()), (m[3].lower(), b.strip().lower()))))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keys-run", default="frozen")
    ap.add_argument("--joins-run", default="default")
    ap.add_argument("--stage3-run")
    ap.add_argument("--fiben-sql", default=str(ROOT / "data/fiben/FIBEN.sql"))
    args = ap.parse_args()
    db = json.loads((ROOT / "out/filtered/fiben/databases.json").read_text())[0]
    con, schema = open_db(db["duckdb_file"]), db["duckdb_schema"]
    pks = {t.lower(): [c.lower() for c in cols] for t, cols in con.execute(
        "SELECT table_name, constraint_column_names FROM duckdb_constraints() "
        "WHERE schema_name = ? AND constraint_type = 'PRIMARY KEY'", [schema]).fetchall()}
    fks = sql_fks(args.fiben_sql) if Path(args.fiben_sql).exists() else None
    questions = [json.loads(l) for l in open(ROOT / "out/filtered/fiben/questions.jsonl")]
    qj = query_joins(questions, db["db_id"])
    profile = json.loads((ROOT / f"out/profiles/fiben/{db['db_id']}.json").read_text())
    keys = json.loads((ROOT / f"out/keys/fiben/{args.keys_run}/{db['db_id']}.json").read_text())
    s3 = ROOT / f"out/descriptions/fiben/{args.stage3_run}" if args.stage3_run else None
    jpath = s3 / "joins" / f"{db['db_id']}.json" if s3 else None
    joins = json.loads((jpath if jpath and jpath.exists() else
                        ROOT / f"out/joins/fiben/{args.joins_run}/{db['db_id']}.json").read_text())

    c = Counter()
    for t, k in keys["tables"].items():
        g, rows = pks.get(t.lower(), []), profile["tables"][t]["rows"]
        pred = None if k.get("evidence") == "name_only" else k["primary_key"]
        c["predicted"] += pred is not None
        if len(g) == 1:
            ok = (pred or "").lower() == g[0]
            c["single"] += 1
            c["correct"] += ok
            c["single_with_rows"] += rows > 0
            c["correct_with_rows"] += ok and rows > 0
        c["name_only"] += k.get("evidence") == "name_only"
        c["name_only_correct"] += k.get("evidence") == "name_only" and len(g) == 1 and k["primary_key"].lower() == g[0]
    pk = {**prf(c["correct"], c["predicted"], c["single"]),
          "recall_tables_with_rows": round(c["correct_with_rows"] / c["single_with_rows"], 4) if c["single_with_rows"] else None,
          "single_key_tables_with_rows": c["single_with_rows"],
          "r5_name_only": {"predicted": c["name_only"], "correct": c["name_only_correct"]}}

    pair = lambda r: frozenset(((r["source"]["table"].lower(), r["source"]["column"].lower()),
                                (r["target"]["table"].lower(), r["target"]["column"].lower())))
    jg = {}
    for g in ("direct", "stats", "llm"):
        if g in joins["graphs"]:
            e = {pair(x) for x in joins["graphs"][g]}
            jg[g] = {"edges": len(e),
                     "query_join_recall": round(len(e & set(qj)) / len(qj), 4) if qj else None,
                     "query_join_recall_weighted": round(sum(n for p, n in qj.items() if p in e) / sum(qj.values()), 4) if qj else None}
            if fks is not None:
                jg[g]["vs_declared_fks"] = prf(len(e & fks), len(e), len(fks))
    verdicts = Counter()
    for r in joins["candidates"]:
        if r["decision"] == "band" or r["decision"].startswith("llm_"):
            p = pair(r)
            verdicts[f"{r['decision']}|{'fk' if fks and p in fks else 'query_join' if p in qj else 'other'}"] += 1
    out = {"primary_keys": pk, "query_join_pairs": len(qj), "declared_fks": len(fks) if fks is not None else None,
           "joins": jg, "band_verdicts": dict(sorted(verdicts.items()))}
    if s3 and (s3 / f"{db['db_id']}.json").exists():
        d = json.loads((s3 / f"{db['db_id']}.json").read_text())
        out["descriptions"] = dict(Counter(col["status"] for t in d["tables"].values() for col in t["columns"].values()))
        out["descriptions_on_tables_with_rows"] = dict(Counter(
            col["status"] for t, td in d["tables"].items() if profile["tables"][t]["rows"] for col in td["columns"].values()))
    print(json.dumps(out, indent=1))
    if s3:
        (s3 / "grade.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
