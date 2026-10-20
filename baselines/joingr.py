"""Thin adapter over the JoinGR repo. We import its search and metric code as-is
so every number stays comparable to the paper; nothing in JoinGR is edited."""

import json
import pickle
import sys
from pathlib import Path

import faiss

ROOT = Path(__file__).resolve().parents[1]
JOINGR_ROOT = ROOT.parent / "JoinGR-E656"
ARTIFACTS = ROOT / "artifacts"
RESULTS = ROOT / "results"

sys.path.insert(0, str(JOINGR_ROOT / "joingraph"))

from search_heuristic import (  # noqa: E402
    embed_query,
    search,
    search_anchor_candidates,
    TOP_K,
    NUM_ANCHORS,
    MAX_HOPS,
    ALPHA,
    BETA,
    LAMBDA_MMR,
)
from search_eval_base import (  # noqa: E402
    precision_at_k,
    recall_at_k,
    perfect_recall_at_k,
    f1_at_k,
    extract_table_name,
)

ENCODERS = {
    "minilm": "all-MiniLM-L6-v2",
    "bge": "BAAI/bge-large-en-v1.5",
}

DATASETS = {
    "beaver": {
        "dir": JOINGR_ROOT / "data/beaver/dw",
        "nodes": "dw_node_descriptors.json",
        "edges": ["dw_join_edge_descriptors.json", "dw_intra_edge_descriptors.json"],
        "table_node_map": "dw_table_node_map.json",
        "edge_to_nodes": "dw_edge_to_nodes_map.json",
        "node_to_edges": "dw_node_to_edges_map.json",
        "queries": "dev_dw.json",
    },
    "bird": {
        "dir": JOINGR_ROOT / "data/bird/dev",
        "nodes": "bird_dev_node_descriptors.json",
        "edges": ["bird_dev_edge_descriptors.json"],
        "table_node_map": "bird_dev_table_node_map.json",
        "edge_to_nodes": "bird_dev_edge_to_nodes_map.json",
        "node_to_edges": "bird_dev_node_to_edges_map.json",
        "queries": "dev_ground_truth.json",
    },
    # Dev subset (20 DBs, 81 tables), matching the paper's Table 1; data/spider/spider_* is the full train graph.
    "spider": {
        "dir": JOINGR_ROOT / "data/spider/dev",
        "nodes": "spider_dev_node_descriptors.json",
        "edges": ["spider_dev_edge_descriptors.json"],
        "table_node_map": "spider_dev_table_node_map.json",
        "edge_to_nodes": "spider_dev_edge_to_nodes_map.json",
        "node_to_edges": "spider_dev_node_to_edges_map.json",
        "queries": "dev_ground_truth.json",
    },
    # TODO(phase 2+): KaggleDBQA and FIBEN need graphs built from raw tables first.
}

GRAPH_FILES = ("nodes", "edges", "table_node_map", "edge_to_nodes", "node_to_edges", "queries")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_source_graph(dataset):
    cfg = DATASETS[dataset]
    d = cfg["dir"]
    edges = {}
    for name in cfg["edges"]:
        edges.update(_read(d / name))
    return {
        "nodes": _read(d / cfg["nodes"]),
        "edges": edges,
        "table_node_map": _read(d / cfg["table_node_map"]),
        "edge_to_nodes": _read(d / cfg["edge_to_nodes"]),
        "node_to_edges": _read(d / cfg["node_to_edges"]),
        "queries": _read(d / cfg["queries"]),
    }


def graph_dir(dataset, setting):
    return ARTIFACTS / "graphs" / dataset / setting


def embeddings_dir(dataset, setting, encoder):
    return ARTIFACTS / "embeddings" / dataset / setting / encoder


def load_graph(dataset, setting):
    d = graph_dir(dataset, setting)
    return {name: _read(d / f"{name}.json") for name in GRAPH_FILES}


def save_graph(graph, dataset, setting):
    d = graph_dir(dataset, setting)
    d.mkdir(parents=True, exist_ok=True)
    for name in GRAPH_FILES:
        with open(d / f"{name}.json", "w", encoding="utf-8") as f:
            json.dump(graph[name], f, ensure_ascii=False)
    return d


def load_embeddings(dataset, setting, encoder):
    d = embeddings_dir(dataset, setting, encoder)
    node_ids = _read(d / "node_ids.json")
    with open(d / "node_emb_by_id.pkl", "rb") as f:
        node_emb_by_id = pickle.load(f)
    with open(d / "edge_emb_by_id.pkl", "rb") as f:
        edge_emb_by_id = pickle.load(f)
    index = faiss.read_index(str(d / "node_index.faiss"))
    return node_ids, node_emb_by_id, index, edge_emb_by_id
