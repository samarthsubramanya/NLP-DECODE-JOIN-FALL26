"""Minimal export to JoinGR's graph format, only so retrieval can be measured.

Stage 4 (refinement with neighbour evidence, edge purposes) is not part of the PoC.
TODO(stage 4): one refinement pass, edge_purpose text, never promoting rejected joins.

Follows JoinGR's own construction: a node per column; keys are the primary key plus
both ends of every exported join; intra edges run from each non-key column to its
table's keys; join edges are the exported joins.

Variants: "none" (no joins: the stripped floor), "stats", "llm", and "+desc" to fill
semantic names and purposes from Stage 3.
"""

import hashlib


def node_id(name):
    return hashlib.sha256(name.encode()).hexdigest()[:16]


def edge_id(a, b):
    lo, hi = sorted((a, b))
    return hashlib.sha256(f"{lo}|{hi}".encode()).hexdigest()[:16]


def available(variant, joins, descriptions):
    graph, _, desc = variant.partition("+")
    if graph not in ("none", "stats", "llm") or (graph != "none" and graph not in joins["graphs"]):
        return False
    return not desc or descriptions is not None


def build(dbs, variant, cfg):
    """dbs: {db_id: {"profiles", "keys", "joins", "descriptions"}} -> JoinGR graph dict."""
    graph, _, desc = variant.partition("+")
    nodes, edges, by_table, by_node = {}, {}, {}, {}

    for db, out in dbs.items():
        profiles, keys, joins = out["profiles"], out["keys"], out["joins"]
        described = out.get("descriptions") if desc else None
        chosen = [] if graph == "none" else joins["graphs"][graph]
        join_ends = {(e[s]["table"], e[s]["column"]) for e in chosen for s in ("source", "target")}

        nid_of = {}
        for table, tp in profiles["tables"].items():
            table_key = cfg["export"]["table_key"].format(db=db, table=table)
            tdesc = (described or {}).get("tables", {}).get(table, {})
            all_nodes, key_nodes = [], []
            for col, prof in tp["columns"].items():
                name = f"{table_key}.{col}"
                nid = node_id(name)
                is_key = keys["tables"][table]["primary_key"] == col or (table, col) in join_ends
                cdesc = tdesc.get("columns", {}).get(col, {})
                nodes[nid] = {
                    "node_id": nid, "node_name": name, "db_id": db,
                    "table_name": table_key, "semantic_table_name": tdesc.get("table", {}).get("name", ""),
                    "column_name": col, "semantic_column_name": cdesc.get("name", ""),
                    "column_purpose": cdesc.get("description", ""),
                    "column_type": prof["declared_type"], "is_key": is_key,
                }
                nid_of[(table, col)] = nid
                all_nodes.append(nid)
                if is_key:
                    key_nodes.append(nid)
            by_table[table_key] = {"key_nodes": key_nodes, "all_nodes": all_nodes}
            for nid in all_nodes:
                by_node[nid] = {"table_name": table_key, "is_key": nid in key_nodes, "key_nodes": key_nodes, "all_nodes": all_nodes}
                if nid not in key_nodes:
                    for kid in key_nodes:
                        _edge(edges, "intra", nodes[nid], nodes[kid])

        for e in chosen:
            a = nid_of[(e["source"]["table"], e["source"]["column"])]
            b = nid_of[(e["target"]["table"], e["target"]["column"])]
            _edge(edges, "join", nodes[a], nodes[b])

    edge_to_nodes, node_to_edges = {}, {}
    for eid, e in edges.items():
        edge_to_nodes[eid] = {k: e[k] for k in ("node1_id", "node1_name", "node2_id", "node2_name", "edge_type", "edge_purpose")}
        node_to_edges.setdefault(e["node1_id"], []).append(eid)
        node_to_edges.setdefault(e["node2_id"], []).append(eid)
    return {
        "nodes": nodes, "edges": edges,
        "table_node_map": {"by_table": by_table, "by_node": by_node},
        "edge_to_nodes": edge_to_nodes, "node_to_edges": node_to_edges,
    }


def _edge(edges, etype, n1, n2):
    eid = edge_id(n1["node_id"], n2["node_id"])
    if eid in edges:
        return
    edges[eid] = {
        "edge_id": eid, "edge_type": etype, "edge_purpose": "",
        "node1_id": n1["node_id"], "node1_name": n1["node_name"],
        "node1_semantic_table_name": n1["semantic_table_name"], "node1_semantic_column_name": n1["semantic_column_name"],
        "node2_id": n2["node_id"], "node2_name": n2["node_name"],
        "node2_semantic_table_name": n2["semantic_table_name"], "node2_semantic_column_name": n2["semantic_column_name"],
    }
