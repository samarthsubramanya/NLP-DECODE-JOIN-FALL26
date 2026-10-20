"""Embeds a setting's nodes and edges with the same text templates JoinGR uses."""

import argparse
import pickle

from sentence_transformers import SentenceTransformer

from baselines.joingr import ENCODERS, embeddings_dir, load_graph
from beaver_index_builder import _node_text, _edge_text, save_node_embeddings, save_edge_embeddings


def build(dataset, setting, encoder):
    g = load_graph(dataset, setting)
    model = SentenceTransformer(ENCODERS[encoder])
    out = embeddings_dir(dataset, setting, encoder)

    nids = list(g["nodes"])
    vecs = model.encode([_node_text(g["nodes"][n]) for n in nids], batch_size=64, show_progress_bar=True)
    save_node_embeddings(dict(zip(nids, vecs)), directory=str(out))

    eids = list(g["edges"])
    if eids:
        vecs = model.encode([_edge_text(g["edges"][e]) for e in eids], batch_size=64, show_progress_bar=True)
        save_edge_embeddings(dict(zip(eids, vecs)), directory=str(out))
    else:
        with open(out / "edge_emb_by_id.pkl", "wb") as f:
            pickle.dump({}, f)

    print(f"{dataset}/{setting}/{encoder}: {len(nids)} nodes, {len(eids)} edges -> {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", default="beaver")
    p.add_argument("--setting", default="documented")
    p.add_argument("--encoder", nargs="+", default=list(ENCODERS), choices=ENCODERS)
    args = p.parse_args()

    for encoder in args.encoder:
        build(args.dataset, args.setting, encoder)
