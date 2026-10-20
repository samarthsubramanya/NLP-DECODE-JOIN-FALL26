"""Builds the evaluation settings from JoinGR's documented graph.

documented  the graph as JoinGR ships it (upper reference)
no_desc     descriptions withheld, declared keys and joins kept; not one of the
            proposal's settings, but it splits the stripped loss into meaning vs structure
stripped    descriptions, declared keys and every edge derived from them withheld;
            table names, column names and types remain
obfuscated  stripped + opaque column names (not built yet)
"""

import argparse
import copy

from baselines.joingr import load_source_graph, save_graph

NODE_DESC_FIELDS = ("semantic_table_name", "semantic_column_name", "column_purpose")
EDGE_DESC_FIELDS = (
    "edge_purpose",
    "node1_semantic_table_name",
    "node1_semantic_column_name",
    "node2_semantic_table_name",
    "node2_semantic_column_name",
)


def drop_descriptions(g):
    for node in g["nodes"].values():
        for field in NODE_DESC_FIELDS:
            node[field] = ""
    for edge in g["edges"].values():
        for field in EDGE_DESC_FIELDS:
            if field in edge:
                edge[field] = ""
    for meta in g["edge_to_nodes"].values():
        meta["edge_purpose"] = ""


def drop_keys(g):
    # Join edges come from declared FKs and intra edges hang off the declared PK,
    # so neither survives once keys are withheld.
    for node in g["nodes"].values():
        node["is_key"] = False
    for entry in g["table_node_map"]["by_table"].values():
        entry["key_nodes"] = []
    for entry in g["table_node_map"]["by_node"].values():
        entry["key_nodes"] = []
        entry["is_key"] = False
    g["edges"] = {}
    g["edge_to_nodes"] = {}
    g["node_to_edges"] = {}


def obfuscate_names(g):
    # TODO(phase 2): replace table/column names with opaque ids, write the
    # original -> opaque mapping, and push gold tables/columns through the same map.
    raise NotImplementedError("obfuscated setting comes after the stripped baseline")


SETTINGS = {
    "documented": [],
    "no_desc": [drop_descriptions],
    "stripped": [drop_descriptions, drop_keys],
    "obfuscated": [drop_descriptions, drop_keys, obfuscate_names],
}


def build(dataset, setting):
    g = copy.deepcopy(load_source_graph(dataset))
    for step in SETTINGS[setting]:
        step(g)
    return save_graph(g, dataset, setting), g


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", default="beaver")
    p.add_argument("--setting", nargs="+", default=["documented"], choices=SETTINGS)
    args = p.parse_args()

    for setting in args.setting:
        out, g = build(args.dataset, setting)
        n_join = sum(1 for e in g["edge_to_nodes"].values() if e.get("edge_type") == "join")
        n_keys = sum(1 for n in g["nodes"].values() if n.get("is_key"))
        print(f"{args.dataset}/{setting}: {len(g['nodes'])} nodes, {len(g['edges'])} edges "
              f"({n_join} join), {n_keys} key columns, {len(g['queries'])} queries -> {out}")
