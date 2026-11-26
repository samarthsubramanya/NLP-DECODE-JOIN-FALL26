"""Memorization probe, step 2 (hp): the plain LLM against our method, on benchmark questions and on new ones.

Each question is answered by the same frozen LLM in three ways; it returns the tables and columns needed:
    closed_book  question + database name only, no schema: anything right here comes from memory
    plain        question + the schema as this setting shows it (stripped: real names; obfuscated: col_NNN)
    ours         question + the catalog DECODE-JOIN exported for JoinGR in this setting: every column's
                 recovered name and purpose, plus the recovered join edges (artifacts/graphs/<benchmark>/<graph>)
Two question sets:
    gold  --gold-per-db benchmark questions per database (2+ gold tables, as JoinGR is scored), fixed seed
    new   evals/memorization/questions/<benchmark>.jsonl (questions.py), reviewed ones only by default

Reading: closed_book scoring well on gold but not on new is memorization. If plain drops from gold to new by about
as much as closed_book, its benchmark score was partly memory; if ours drops less, the recovered catalog carries
the information that memory supplied. Table names are kept in every setting, so column recall is the sharper
measure in the obfuscated setting.

    python evals/memorization/probe.py --dataset bird_obfuscated --ours-graph ddb__bird_obfuscated__ddb_full
    python evals/memorization/probe.py --dataset fiben_stripped --arms closed_book plain        # no exported graph yet

Writes out/memorization/<dataset>/probe.jsonl (one row per question x arm) and prints a markdown table.
"""
import argparse
import json
import random
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "evals"), str(ROOT / "evals" / "sparsity")]
import labels                                    # noqa: E402
from common import documented, source_file, work_list   # noqa: E402

OUT = ROOT / "out" / "memorization"
QDIR = Path(__file__).resolve().parent / "questions"
ARMS = ("closed_book", "plain", "ours")

SYSTEM = """You map a question about a relational database to the tables and columns a SQL query needs to answer it.
Use the exact table and column names you are given; if you are given no schema, use the names you believe the
database has. List every table the query reads (including tables only needed to join others) and the columns it
uses in SELECT, WHERE, GROUP BY, ORDER BY and JOIN conditions. Return JSON only."""

ANSWER = {"name": "needed_schema", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["tables", "columns"],
    "properties": {"tables": {"type": "array", "items": {"type": "string"}},
                   "columns": {"type": "array", "items": {
                       "type": "object", "additionalProperties": False, "required": ["table", "column"],
                       "properties": {"table": {"type": "string"}, "column": {"type": "string"}}}}}}}


def last(name):
    return str(name).rsplit(".", 1)[-1].lower()


# ------------------------------------------------------------------ question sets
def gold_questions(base, per_db, seed):
    from baselines.joingr import load_graph
    queries = load_graph(base, "documented")["queries"]
    gold = json.loads((ROOT / "gold" / f"{base}.json").read_text())
    by_db = defaultdict(list)
    for g in gold:
        qq = queries[g["i"]]
        tables = {last(t) for t in g["gold_tables"]}
        if g["status"] != "ok" or len(tables) < 2 or not qq.get("question"):
            continue
        cols = sorted({".".join(n.split(".")[-2:]).lower() for n in g["gold_column_names"]})
        db = qq.get("db_id") or "dw"
        by_db[db].append({"uid": f"{base}:gold:{g['i']}", "db_id": db, "question": qq["question"],
                          "gold_tables": sorted(tables), "gold_columns": cols, "set": "gold"})
    rng, out = random.Random(seed), []
    for db in sorted(by_db):
        out += rng.sample(by_db[db], min(per_db, len(by_db[db])))
    return out


def new_questions(base, include_unreviewed):
    path = QDIR / f"{base}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        r = json.loads(line)
        if r.get("reviewed") or include_unreviewed:
            out.append({"uid": r["uid"], "db_id": r["db_id"], "question": r["question"], "set": "new",
                        "gold_tables": sorted(t.lower() for t in r["gold_tables"]),
                        "gold_columns": sorted(c.lower() for c in r.get("gold_columns", []))})
    return out


# ------------------------------------------------------------------ what each arm is shown
def shown_schemas(dataset):
    """{db_id: {table: [column, ...]}} as the setting's DuckDB file shows them."""
    import duckdb
    dbs = work_list(dataset)
    con = duckdb.connect(str(source_file(dbs[0]["duckdb_file"])), read_only=True)
    out = {}
    for db in dbs:
        tables = defaultdict(list)
        for t, c in con.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = ? "
                                "ORDER BY table_name, ordinal_position", [db["duckdb_schema"]]).fetchall():
            if t in db["tables"]:
                tables[t].append(c)
        out[db["db_id"]] = dict(tables)
    con.close()
    return out


def our_catalogs(base, graph):
    """{db (lower): {"tables": {table: {column: "name: purpose"}}, "joins": ["t.c = t.c"]}} from an exported graph."""
    d = ROOT / "artifacts" / "graphs" / base / graph
    nodes = json.loads((d / "nodes.json").read_text())
    edges = json.loads((d / "edges.json").read_text())
    out = defaultdict(lambda: {"tables": defaultdict(dict), "joins": []})
    for n in nodes.values():
        db = str(n.get("db_id") or "dw").lower()
        t = last(n["table_name"])
        meaning = " — ".join(x for x in (n.get("semantic_column_name"), n.get("column_purpose")) if x)
        out[db]["tables"][t][n["column_name"]] = meaning
    for e in edges.values():
        if e.get("edge_type") != "join":
            continue
        a, b = nodes[e["node1_id"]], nodes[e["node2_id"]]
        db = str(a.get("db_id") or "dw").lower()
        out[db]["joins"].append(f"{last(a['table_name'])}.{a['column_name']} = {last(b['table_name'])}.{b['column_name']}")
    return out


def prompt(arm, q, schemas, catalogs):
    base = {"database": q["db_id"], "question": q["question"]}
    if arm == "closed_book":
        return {**base, "schema": "not shown"}
    if arm == "plain":
        return {**base, "schema": schemas[q["db_id"]]}
    cat = catalogs.get(q["db_id"].lower())
    if cat is None:
        return None
    return {**base, "schema": {t: cols for t, cols in cat["tables"].items()},
            "schema_note": "each column is shown as name: recovered meaning", "joins": cat["joins"]}


# ------------------------------------------------------------------ scoring
def to_real(dataset, mapping, db, table, column, arm):
    """Predicted names -> real lower-case (table, column). Closed-book answers are already real names."""
    t = last(table)
    if not mapping or arm == "closed_book" or db not in mapping:
        return t, str(column).lower()
    m = mapping[db]
    rt = next((k for k in m["columns"] if k.lower() == t), None)
    if rt is None:
        return t, str(column).lower()
    inv = {v.lower(): k for k, v in m["columns"][rt].items()}
    return t, inv.get(str(column).lower(), str(column)).lower()


def score(q, reply, dataset, mapping, arm):
    reply = reply or {}
    tables = {last(t) for t in reply.get("tables") or []}
    cols = {"{}.{}".format(*to_real(dataset, mapping, q["db_id"], c.get("table", ""), c.get("column", ""), arm))
            for c in reply.get("columns") or [] if isinstance(c, dict)}
    gt, gc = set(q["gold_tables"]), set(q["gold_columns"])
    hit = len(tables & gt)
    return {"table_recall": hit / len(gt) if gt else None,
            "table_precision": hit / len(tables) if tables else 0.0,
            "all_tables": float(gt <= tables) if gt else None,
            "column_recall": len(cols & gc) / len(gc) if gc else None,
            "predicted_tables": sorted(tables), "predicted_columns": sorted(cols), "ok": bool(reply)}


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, help="setting to show plain/ours, e.g. bird_obfuscated")
    ap.add_argument("--arms", nargs="+", default=list(ARMS), choices=ARMS)
    ap.add_argument("--ours-graph", help="exported graph folder under artifacts/graphs/<benchmark>/")
    ap.add_argument("--gold-per-db", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--include-unreviewed", action="store_true")
    ap.add_argument("--model", default="glm-5-3")
    ap.add_argument("--reasoning-effort", default="low", choices=["none", "low", "medium", "high"])
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    if "ours" in args.arms and not args.ours_graph:
        ap.error("--ours-graph is needed for the ours arm (or leave ours out of --arms)")

    from describe_columns import DEFAULTS as LLM, Voyager
    base = documented(args.dataset)
    qs = gold_questions(base, args.gold_per_db, args.seed) + new_questions(base, args.include_unreviewed)
    if not any(q["set"] == "new" for q in qs):
        print(f"note: no {'reviewed ' if not args.include_unreviewed else ''}new questions for {base} yet")
    schemas = shown_schemas(args.dataset)
    catalogs = our_catalogs(base, args.ours_graph) if "ours" in args.arms else {}
    mapping = None
    if args.dataset.endswith("_obfuscated"):
        import duckdb
        dbs = work_list(base)
        con = duckdb.connect(str(source_file(dbs[0]["duckdb_file"])), read_only=True)
        mapping = labels.position_mapping(con, {d["db_id"]: d["duckdb_schema"] for d in dbs})
        con.close()

    out_dir = OUT / args.dataset
    out_dir.mkdir(parents=True, exist_ok=True)
    llm = Voyager({**LLM, "model": args.model, "reasoning_effort": args.reasoning_effort}, out_dir / "llm_calls.jsonl")
    jobs = [(arm, q) for q in qs for arm in args.arms]

    def run(job):
        arm, q = job
        p = prompt(arm, q, schemas, catalogs)
        if p is None:
            return None
        reply, _ = llm.complete(f"probe_{arm}", q["uid"], SYSTEM, json.dumps(p, ensure_ascii=False), ANSWER)
        return {"dataset": args.dataset, "arm": arm, "set": q["set"], "uid": q["uid"], "db_id": q["db_id"],
                "gold_tables": q["gold_tables"], "gold_columns": q["gold_columns"],
                **score(q, reply, args.dataset, mapping, arm)}

    with ThreadPoolExecutor(args.workers) as pool:
        rows = [r for r in pool.map(run, jobs) if r]
    (out_dir / "probe.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    keys = ("table_recall", "all_tables", "column_recall")
    print(f"| Arm | Set | n | {' | '.join(keys)} |\n|---|---|---|" + "---|" * len(keys))
    for arm in args.arms:
        for s in ("gold", "new"):
            rs = [r for r in rows if r["arm"] == arm and r["set"] == s]
            if rs:
                vals = [mean([r[k] for r in rs]) for k in keys]
                print(f"| {arm} | {s} | {len(rs)} | " + " | ".join("–" if v is None else f"{v:.3f}" for v in vals) + " |")
    print("\nGap (gold − new), per arm:")
    for arm in args.arms:
        g = [mean([r[k] for r in rows if r["arm"] == arm and r["set"] == s]) for s in ("gold", "new") for k in keys]
        if None not in (g[0], g[3]):
            print(f"  {arm}: table recall {g[0] - g[3]:+.3f}" +
                  (f", column recall {g[2] - g[5]:+.3f}" if None not in (g[2], g[5]) else ""))


if __name__ == "__main__":
    main()
