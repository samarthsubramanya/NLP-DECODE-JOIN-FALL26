"""Grade a BEAVER run of Stages 2a, 2b and 3.

BEAVER's DuckDB tables carry no constraints, but the importer keeps each table's original MySQL DDL in
beaver_metadata.source_tables (definition_json -> constraints). Those are the evaluation labels, read only here:
    primary keys  "PRIMARY KEY (`a`)" lines (nova and neutron declare one for every table; dw declares none)
    foreign keys  "CONSTRAINT ... FOREIGN KEY (`a`) REFERENCES `t` (`b`)" lines (nova, neutron; none in dw)
    query joins   column pairs the gold SQL joins on (a = b between two base tables, aliases resolved with
                  sqlglot), from out/filtered/beaver/questions.jsonl; a recall-only label: real foreign keys
                  that no question uses are missing from it, so precision against it means little
BEAVER ships no column descriptions, so Stage 3 descriptions are only counted by status.

Usage (from the repo root):
    python evals/grade_beaver.py [--databases nova] [--keys-run frozen] [--joins-run default] [--stage3-run R]

Output: out/descriptions/beaver/<stage3-run>/grade.json when --stage3-run is given, and a printed summary.
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from duckdb_driver import open_db   # noqa: E402

FK_RE = re.compile(r"FOREIGN KEY \(([^)]*)\) REFERENCES `([^`]*)` \(([^)]*)\)")
cols_of = lambda s: [c.strip().strip("`").lower() for c in s.split(",")]


def ddl_labels(con, db):
    """({table: [pk cols]}, {frozenset of two (table, column)}) from the original MySQL DDL."""
    pks, fks = {}, set()
    for t, d in con.execute("SELECT table_name, definition_json FROM beaver_metadata.source_tables WHERE source_db = ?",
                            [db]).fetchall():
        for line in json.loads(d).get("constraints", []):
            if line.startswith("PRIMARY KEY"):
                pks[t.lower()] = cols_of(line[line.index("(") + 1:line.rindex(")")])
            m = FK_RE.search(line)
            if m:
                for a, b in zip(cols_of(m[1]), cols_of(m[3])):
                    fks.add(frozenset(((t.lower(), a), (m[2].lower(), b))))
    return pks, fks


def query_joins(questions, db):
    """{frozenset pair: number of questions using it}."""
    count = Counter()
    for q in questions:
        if q["db_id"] != db:
            continue
        try:
            tree = sqlglot.parse_one(q["gold_sql"], read=q.get("dialect") or "mysql")
        except sqlglot.errors.ParseError:
            continue
        pairs = set()
        for scope in traverse_scope(tree):
            tables, s = {}, scope   # this scope's tables, then outer ones (correlated subqueries)
            while s:
                for a, src in s.sources.items():
                    if isinstance(src, exp.Table):
                        tables.setdefault(a, src.name.lower())
                s = s.parent
            own = [src for src in scope.sources.values() if isinstance(src, exp.Table)]
            only = own[0].name.lower() if len(own) == 1 else None   # an unqualified column needs one own table
            for eq in scope.expression.find_all(exp.EQ):
                l, r = eq.left, eq.right
                if isinstance(l, exp.Column) and isinstance(r, exp.Column):
                    tl, tr = tables.get(l.table, only if not l.table else None), tables.get(r.table, only if not r.table else None)
                    if tl and tr and tl != tr:
                        pairs.add(frozenset(((tl, l.name.lower()), (tr, r.name.lower()))))
        count.update(pairs)
    return count


def prf(tp, pred, gold):
    p, r = (tp / pred if pred else 0.0), (tp / gold if gold else 0.0)
    return {"tp": tp, "predicted": pred, "gold": gold, "precision": round(p, 4), "recall": round(r, 4),
            "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--databases", nargs="*")
    ap.add_argument("--keys-run", default="frozen")
    ap.add_argument("--joins-run", default="default")
    ap.add_argument("--stage3-run", help="grade out/descriptions/beaver/<run> (its joins/ with graphs.llm)")
    args = ap.parse_args()
    dbs = [d for d in json.loads((ROOT / "out/filtered/beaver/databases.json").read_text())
           if not args.databases or d["db_id"] in args.databases]
    questions = [json.loads(l) for l in open(ROOT / "out/filtered/beaver/questions.jsonl")]
    report = {"keys_run": args.keys_run, "joins_run": args.joins_run, "stage3_run": args.stage3_run, "per_db": {}}
    for db in dbs:
        db_id = db["db_id"]
        con = open_db(db["duckdb_file"])
        pks, fks = ddl_labels(con, db_id)
        qj = query_joins(questions, db_id)
        profile = json.loads((ROOT / f"out/profiles/beaver/{db_id}.json").read_text())
        keys = json.loads((ROOT / f"out/keys/beaver/{args.keys_run}/{db_id}.json").read_text())
        s3 = ROOT / f"out/descriptions/beaver/{args.stage3_run}" if args.stage3_run else None
        jpath = s3 / "joins" / f"{db_id}.json" if s3 else None
        joins = json.loads((jpath if jpath and jpath.exists() else
                            ROOT / f"out/joins/beaver/{args.joins_run}/{db_id}.json").read_text())

        # primary keys
        c, lost = Counter(), Counter()
        for t, k in keys["tables"].items():
            g = pks.get(t.lower(), [])
            rows = profile["tables"][t]["rows"]
            pred = None if k.get("evidence") == "name_only" else k["primary_key"]
            c["predicted"] += pred is not None
            if len(g) == 1:
                c["single"] += 1
                c["single_with_rows"] += rows > 0
                ok = (pred or "").lower() == g[0]
                c["correct"] += ok
                c["correct_with_rows"] += ok and rows > 0
                if not ok:
                    col = next((v for n, v in k["columns"].items() if n.lower() == g[0]), {})
                    lost["empty table" if rows == 0 else (col.get("rejected") or "scored below accept").split(":")[0]] += 1
            elif len(g) > 1:
                c["composite"] += 1
        pk = {**prf(c["correct"], c["predicted"], c["single"]),
              "recall_tables_with_rows": round(c["correct_with_rows"] / c["single_with_rows"], 4) if c["single_with_rows"] else None,
              "single_key_tables_with_rows": c["single_with_rows"], "composite_key_tables": c["composite"],
              "empty_tables": sum(t["rows"] == 0 for t in profile["tables"].values()), "gold_lost_because": dict(lost)}

        # joins
        def edges(g):
            return {frozenset(((e["source"]["table"].lower(), e["source"]["column"].lower()),
                               (e["target"]["table"].lower(), e["target"]["column"].lower()))) for e in joins["graphs"][g]}
        jg = {}
        for g in ("direct", "stats", "llm"):
            if g in joins["graphs"]:
                e = edges(g)
                jg[g] = {"vs_declared_fks": prf(len(e & fks), len(e), len(fks)),
                         "query_join_recall": round(len(e & set(qj)) / len(qj), 4) if qj else None,
                         "query_join_recall_weighted": round(sum(n for p, n in qj.items() if p in e) / sum(qj.values()), 4) if qj else None}
        verdicts = Counter()
        for r in joins["candidates"]:
            if r["decision"] == "band" or r["decision"].startswith("llm_"):
                pair = frozenset(((r["source"]["table"].lower(), r["source"]["column"].lower()),
                                  (r["target"]["table"].lower(), r["target"]["column"].lower())))
                verdicts[f"{r['decision']}|{'fk' if pair in fks else 'query_join' if pair in qj else 'neither'}"] += 1
        out = {"primary_keys": pk, "declared_fks": len(fks), "query_join_pairs": len(qj), "joins": jg,
               "band_verdicts": dict(sorted(verdicts.items()))}
        if s3 and (s3 / f"{db_id}.json").exists():
            d = json.loads((s3 / f"{db_id}.json").read_text())
            out["descriptions"] = dict(Counter(col["status"] for t in d["tables"].values() for col in t["columns"].values()))
        report["per_db"][db_id] = out
        print(json.dumps({db_id: out}, indent=1))
    if s3:
        (s3 / "grade.json").write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
