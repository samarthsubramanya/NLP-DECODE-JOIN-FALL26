"""Grade a run's joins against our own gold set: the column pairs the gold SQL joins on.

Same questions and SQL as gold/<dataset>.json (baselines/gold_columns.py), pairs extracted with
grade_beaver.query_joins. Recall-only label: a real FK no question uses is missing, so precision is a
lower bound. Only databases that have at least one gold-SQL join are graded.

Usage (from the repo root):
    python evals/grade_gold_sql_joins.py --dataset bird_obfuscated --run ddb_low
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "evals")]
import sqlglot                                                   # noqa: E402
import labels                                                    # noqa: E402
from baselines.gold_columns import DIALECTS, DOUBLE_QUOTED_STRINGS, SQL_FIELDS  # noqa: E402
from baselines.joingr import load_source_graph                   # noqa: E402
from duckdb_driver import open_db                                # noqa: E402
from grade_beaver import prf, query_joins                        # noqa: E402


def gold_questions(base):
    out = []
    for q in load_source_graph(base)["queries"]:
        sql = next((q[f] for f in SQL_FIELDS if q.get(f)), "")
        if base in DOUBLE_QUOTED_STRINGS:
            sql = re.sub(r'"([^"]*)"', r"'\1'", sql)
        try:
            sqlglot.parse_one(sql, read=DIALECTS[base])
        except sqlglot.errors.SqlglotError:      # query_joins only catches ParseError
            print(f"skip unparseable gold SQL: {sql[:60]}", file=sys.stderr)
            continue
        out.append({"db_id": (q.get("db_id") or "dw").lower(), "gold_sql": sql, "dialect": DIALECTS[base]})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--run", required=True, help="out/descriptions/<dataset>/<run>")
    args = ap.parse_args()
    base = labels.documented(args.dataset)
    dbs = json.loads((ROOT / f"out/filtered/{base}/databases.json").read_text())
    schemas = {d["db_id"]: d["duckdb_schema"] for d in dbs}
    mapping = (labels.position_mapping(open_db(dbs[0]["duckdb_file"]), schemas)
               if args.dataset.endswith("_obfuscated") else None)
    questions = gold_questions(base)

    total, per_db = {g: Counter() for g in ("stats", "llm")}, {}
    for db in schemas:
        gold = set(query_joins(questions, db.lower()))
        path = ROOT / f"out/descriptions/{args.dataset}/{args.run}/joins/{db}.json"
        if not gold or not path.exists():
            continue
        joins = json.loads(path.read_text())
        per_db[db] = {}
        for g, c in total.items():
            edges = {labels.norm_edge(mapping, db, e) for e in joins["graphs"][g]}
            tp = len(edges & gold)
            c.update(tp=tp, predicted=len(edges), gold=len(gold))
            per_db[db][g] = prf(tp, len(edges), len(gold))
    print(json.dumps({"dataset": args.dataset, "run": args.run, "dbs": len(per_db),
                      **{g: prf(c["tp"], c["predicted"], c["gold"]) for g, c in total.items()},
                      "per_db": per_db}))


if __name__ == "__main__":
    main()
