"""Scores exported PoC graphs with the existing JoinGR harness (baselines/).

The graph keeps the setting's names in every text field JoinGR embeds, so the
retriever only sees what the pipeline saw. Only the ids used for scoring are mapped
back to the original schema: node ids become the baseline node ids (so gold columns
match) and table keys become the original table keys (so gold tables match).
"""

import json

from baselines import index as bl_index, run as bl_run
from baselines.joingr import RESULTS, load_graph, save_graph


def to_scoring_space(g, baseline, names, queries=None):
    by_name = {n["node_name"].lower(): nid for nid, n in baseline["nodes"].items()}
    base_table = baseline["table_node_map"]["by_node"]

    new_id, new_table, unmatched = {}, {}, 0
    for nid, n in g["nodes"].items():
        table = n["table_name"].split(".", 1)[-1]
        db, otable, ocol = names.to_original(n["db_id"], table, n["column_name"])
        bid = by_name.get(f"{db}.{otable}.{ocol}") or by_name.get(f"{otable}.{ocol}")
        if bid is None:
            unmatched += 1
            bid = nid
        new_id[nid] = bid
        new_table[n["table_name"]] = base_table[bid]["table_name"] if bid in base_table else n["table_name"]

    nodes = {new_id[nid]: {**n, "node_id": new_id[nid]} for nid, n in g["nodes"].items()}
    edges = {eid: {**e, "node1_id": new_id[e["node1_id"]], "node2_id": new_id[e["node2_id"]]} for eid, e in g["edges"].items()}
    edge_to_nodes = {eid: {**e, "node1_id": new_id[e["node1_id"]], "node2_id": new_id[e["node2_id"]]} for eid, e in g["edge_to_nodes"].items()}
    node_to_edges = {new_id[nid]: eids for nid, eids in g["node_to_edges"].items()}

    by_table = {
        new_table[t]: {k: [new_id[x] for x in v] for k, v in entry.items()}
        for t, entry in g["table_node_map"]["by_table"].items()
    }
    by_node = {}
    for nid, entry in g["table_node_map"]["by_node"].items():
        by_node[new_id[nid]] = {
            "table_name": new_table[entry["table_name"]], "is_key": entry["is_key"],
            "key_nodes": [new_id[x] for x in entry["key_nodes"]], "all_nodes": [new_id[x] for x in entry["all_nodes"]],
        }
    out = {"nodes": nodes, "edges": edges, "edge_to_nodes": edge_to_nodes, "node_to_edges": node_to_edges,
           "table_node_map": {"by_table": by_table, "by_node": by_node},
           "queries": baseline["queries"] if queries is None else queries}
    return out, unmatched


def compact(res):
    s, c = res["summary_fixed"], res.get("summary_columns")
    row = {f"{m}@{k}": s[k][m] for k in (5, 15) for m in ("R", "PR")}
    if c:
        row |= {f"col_{m}@{k}": c[k][m] for k in (5, 15) for m in ("R", "PR")}
    return row


def baseline_rows(dataset, setting, encoders, methods):
    """Already-computed baseline scores for one setting, for the recovered-gap comparison."""
    rows = {}
    for enc in encoders:
        for m in methods:
            path = RESULTS / dataset / setting / enc / f"{m}.json"
            if path.exists():
                r = json.loads(path.read_text())
                rows[f"{enc}/{m}"] = compact({"summary_fixed": {int(k): v for k, v in r["summary_fixed"].items()},
                                              "summary_columns": {int(k): v for k, v in r["summary_columns"].items()} if r.get("summary_columns") else None})
    return rows


def run(ctx, variants, log=print):
    cfg = ctx.cfg["evaluate"]["retrieval"]
    dataset = ctx.cfg["data"]["dataset"]
    baseline = load_graph(dataset, "documented")
    results = {}
    for variant in variants:
        src = ctx.run_path / "graphs" / variant
        if not src.exists():
            continue
        g = {p.stem: json.loads(p.read_text()) for p in src.glob("*.json")}
        scored, unmatched = to_scoring_space(g, baseline, ctx.names)
        name = f"poc__{ctx.setting}__{ctx.run_path.name}__{variant.replace('+', '_')}"
        save_graph(scored, dataset, name)
        log(f"retrieval {variant}: {len(scored['nodes'])} nodes ({unmatched} unmatched to the baseline graph)")
        results[variant] = {"unmatched_nodes": unmatched, "scores": {}}
        for enc in cfg["encoders"]:
            bl_index.build(dataset, name, enc)
            for method in cfg["methods"]:
                res, _ = bl_run.evaluate(dataset, name, enc, method)
                results[variant]["scores"][f"{enc}/{method}"] = compact(res)
    references = {s: baseline_rows(dataset, s, cfg["encoders"], cfg["methods"])
                  for s in ("documented", ctx.setting)}
    return {"variants": results, "baselines": references}
