"""Retrieval scores through the JoinGR harness, same questions and metrics for every row.

Run from quartile/, after the README's retrieval setup:
    python evals/score_joingr.py alone <benchmark> <documented|stripped|obfuscated>
    python evals/score_joingr.py pipeline <dataset variant> <run under out/joingr/<dataset>/>

Questions: the harness's list for the benchmark, keeping those with 2+ gold tables; each keeps its index
in the full list so gold columns line up. BEAVER is dw only: it is the only BEAVER database JoinGR has a
graph and gold for.

JoinGR alone: documented is the graph JoinGR ships. Stripped is the harness's stripped graph. Obfuscated
is that graph with each column renamed to its name in the obfuscated DuckDB file (col_NNN by position).
Prints one JSON line.
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, ".")
from poc.evaluate.retrieval import to_scoring_space                     # noqa: E402
from baselines import index as bl_index, run as bl_run                  # noqa: E402
from baselines.joingr import embeddings_dir, load_graph, save_graph     # noqa: E402

Q = Path(__file__).resolve().parent.parent
sys.path[len(sys.path):] = [str(Q / "src"), str(Q / "evals")]
import labels                                                           # noqa: E402
from duckdb_driver import open_db                                       # noqa: E402

ENCODERS, METHODS = ("minilm", "bge"), ("vanilla", "heuristic", "merged")


def real_names(dataset):
    """{db lower: {table lower: {col_NNN: real column}}} for an obfuscated variant, else None."""
    if not dataset.endswith("_obfuscated"):
        return None
    dbs = json.loads((Q / f"out/filtered/{labels.documented(dataset)}/databases.json").read_text())
    m = labels.position_mapping(open_db(dbs[0]["duckdb_file"]), {d["db_id"]: d["duckdb_schema"] for d in dbs})
    return {db.lower(): {t.lower(): {shown: real for real, shown in cols.items()} for t, cols in x["columns"].items()}
            for db, x in m.items()}


class Names:
    def __init__(self, dataset):
        self.real = real_names(dataset)

    def to_original(self, db, table, col):
        if self.real:
            col = self.real[db.lower()][table.lower()][col]
        return db.lower(), table.lower(), col.lower()


def questions(base):
    g = load_graph(base, "documented")
    return g, [{**q, "gold_index": i} for i, q in enumerate(g["queries"]) if len(set(q["gold_tables"])) >= 2]


def run_scores(base, name, n_queries, **extra):
    out = {"benchmark": base, "name": name, "n_queries": n_queries, **extra, "scores": {}}
    for enc in ENCODERS:
        for method in METHODS:
            res, _ = bl_run.evaluate(base, name, enc, method)
            s, c = res["summary_fixed"], res.get("summary_columns") or {}
            out["scores"][f"{enc}/{method}"] = {"R@5": s[5]["R"], "R@15": s[15]["R"], "PR@15": s[15]["PR"],
                                                "colR@15": c.get(15, {}).get("R"), "colPR@15": c.get(15, {}).get("PR")}
    print(json.dumps(out))


def alone(base, setting):
    _, queries = questions(base)
    g = load_graph(base, "stripped" if setting == "obfuscated" else setting)
    name = f"ddb_alone__{setting}"
    missing = 0
    if setting == "obfuscated":
        shown = {}
        for db, tables in real_names(f"{base}_obfuscated").items():
            for t, cols in tables.items():
                for s, real in cols.items():
                    shown[(db, t, real.lower())] = s
        for n in g["nodes"].values():
            db = "dw" if base == "beaver" else n["db_id"].lower()
            s = shown.get((db, n["table_name"].rsplit(".", 1)[-1].lower(), n["column_name"].lower()))
            missing += s is None
            n["column_name"] = s or n["column_name"]
            n["node_name"] = f"{n['node_name'].rsplit('.', 1)[0]}.{n['column_name']}"
    save_graph({**g, "queries": queries}, base, name)
    for enc in ENCODERS:
        if setting == "obfuscated":
            bl_index.build(base, name, enc)
        else:   # same nodes and edges as the harness's setting, only the question list changed
            shutil.copytree(embeddings_dir(base, setting, enc), embeddings_dir(base, name, enc), dirs_exist_ok=True)
    run_scores(base, name, len(queries), setting=setting, nodes_not_in_duckdb=missing)


def pipeline(dataset, run):
    base = labels.documented(dataset)
    baseline, queries = questions(base)
    g = {"nodes": {}, "edges": {}, "edge_to_nodes": {}, "node_to_edges": {},
         "table_node_map": {"by_table": {}, "by_node": {}}}
    for f in sorted((Q / f"out/joingr/{dataset}/{run}").glob("*.json")):
        if f.name.endswith(".refined.json") or (base == "beaver" and f.stem != "dw"):
            continue
        x = json.loads(f.read_text())
        for k in ("nodes", "edges", "edge_to_nodes", "node_to_edges"):
            g[k].update(x[k])
        for k in ("by_table", "by_node"):
            g["table_node_map"][k].update(x["table_node_map"][k])
    scored, unmatched = to_scoring_space(g, baseline, Names(dataset), queries)
    name = f"ddb__{dataset}__{run}"
    save_graph(scored, base, name)
    for enc in ENCODERS:
        bl_index.build(base, name, enc)
    run_scores(base, name, len(queries), dataset=dataset, run=run, nodes=len(scored["nodes"]), unmatched=unmatched,
               tables=len(scored["table_node_map"]["by_table"]),
               join_edges=sum(e["edge_type"] == "join" for e in scored["edges"].values()))


if __name__ == "__main__":
    {"alone": alone, "pipeline": pipeline}[sys.argv[1]](*sys.argv[2:4])
