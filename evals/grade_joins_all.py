"""Join precision / recall / F1 for one dataset variant, any of the five benchmarks.

Gold is always taken from the documented side, never from a run's inputs:
    bird, spider   declared FKs in <dataset>_metadata (labels.gold)
    fiben          FIBEN.sql's FKs; "gold" counts only FKs whose two tables have rows, as grade_fiben.py's
                   vs_fks_with_rows does, and declared_* counts all 159
    beaver         nova, neutron: the original MySQL DDL; dw declares none, so JoinGR's own dw join edges
    kaggledbqa     KaggleDBQA_tables.json, through the graph baselines.build_graph made from it

    python evals/grade_joins_all.py --dataset spider_obfuscated --run ddb_low
Prints one JSON line.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "evals")]
import labels                                   # noqa: E402
from duckdb_driver import open_db               # noqa: E402
from grade_beaver import ddl_labels, prf        # noqa: E402
from grade_fiben import sql_fks                 # noqa: E402

HARNESS = ROOT / "artifacts/graphs"
if not HARNESS.exists():      # graphs built in the older sibling checkout
    HARNESS = ROOT.parent / "decode-join/artifacts/graphs"


def graph_fks(base):
    """{db (lower): {frozenset pair}} from the join edges of the harness's documented graph."""
    d = HARNESS / base / "documented"
    nodes = json.loads((d / "nodes.json").read_text())
    out = {}
    for e in json.loads((d / "edges.json").read_text()).values():
        if e.get("edge_type") != "join":
            continue
        a, b = nodes[e["node1_id"]], nodes[e["node2_id"]]
        pair = frozenset((x["table_name"].rsplit(".", 1)[-1].lower(), x["column_name"].lower()) for x in (a, b))
        out.setdefault(str(a.get("db_id", "dw")).lower(), set()).add(pair)
    return out


def load_gold(dataset):
    """({db: documented schema}, name mapping or None, gold(db) -> {frozenset pair})."""
    base = labels.documented(dataset)
    dbs = json.loads((ROOT / f"out/filtered/{base}/databases.json").read_text())
    schemas = {d["db_id"]: d["duckdb_schema"] for d in dbs}
    con = open_db(dbs[0]["duckdb_file"])
    mapping = labels.position_mapping(con, schemas) if dataset.endswith("_obfuscated") else None
    from_graph = graph_fks(base) if base in ("beaver", "kaggledbqa") else {}

    def gold(db):
        if base in ("bird", "spider"):
            return {frozenset(p) for p in labels.gold(con, f"{base}_metadata", schemas[db])[1]}
        if base == "fiben":
            return sql_fks(ROOT / "data/fiben/FIBEN.sql")
        if base == "beaver" and db != "dw":
            return ddl_labels(con, db)[1]
        return from_graph.get(db.lower(), set())

    return schemas, mapping, gold


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--run", required=True, help="out/descriptions/<dataset>/<run>")
    args = ap.parse_args()
    base = labels.documented(args.dataset)
    schemas, mapping, gold = load_gold(args.dataset)
    total = {g: Counter() for g in ("stats", "llm")}
    per_db, band, declared = {}, Counter(), Counter()
    for db in schemas:
        path = ROOT / f"out/descriptions/{args.dataset}/{args.run}/joins/{db}.json"
        joins = json.loads(path.read_text())
        fks = gold(db)
        if base == "fiben":
            prof = json.loads((ROOT / f"out/profiles/{args.dataset}/{db}.json").read_text())
            rows = {t.lower(): p["rows"] for t, p in prof["tables"].items()}
            declared["gold"] = len(fks)
            declared["tp"] = len({labels.norm_edge(mapping, db, e) for e in joins["graphs"]["llm"]} & fks)
            fks = {f for f in fks if all(rows.get(t, 0) for t, _ in f)}
        per_db[db] = {}
        for g, c in total.items():
            edges = {labels.norm_edge(mapping, db, e) for e in joins["graphs"][g]}
            tp = len(edges & fks)
            c.update(tp=tp, predicted=len(edges), gold=len(fks))
            per_db[db][g] = prf(tp, len(edges), len(fks))
        for r in joins["candidates"]:
            if r["decision"] == "band" or r["decision"].startswith("llm_"):
                band[r["decision"]] += 1
    out = {"dataset": args.dataset, "run": args.run, "band": dict(band),
           **{g: prf(c["tp"], c["predicted"], c["gold"]) for g, c in total.items()}, "per_db": per_db}
    if declared:
        out["declared"] = prf(declared["tp"], total["llm"]["predicted"], declared["gold"])
    print(json.dumps(out))


if __name__ == "__main__":
    main()
