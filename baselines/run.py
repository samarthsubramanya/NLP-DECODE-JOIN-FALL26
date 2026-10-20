"""Scores JoinGR baselines on one dataset/setting/encoder and writes results JSON.

vanilla    dense retrieval: tables ranked by their best-matching column
heuristic  JoinGR's untrained walk with the repo's default settings
trained    JoinGR's learned scorer (not wired in yet)
"""

import argparse
import json
import time

from sentence_transformers import SentenceTransformer

from baselines.joingr import (
    ENCODERS, RESULTS, TOP_K, NUM_ANCHORS, MAX_HOPS, ALPHA, BETA, LAMBDA_MMR,
    embed_query, search, search_anchor_candidates, extract_table_name,
    precision_at_k, recall_at_k, perfect_recall_at_k, f1_at_k,
    load_graph, load_embeddings,
)

KS = [5, 10, 15]


def rank_vanilla(question, model, ctx):
    qv = embed_query(question, model)
    cands = search_anchor_candidates(qv, ctx["node_ids"], ctx["index"], ctx["tnm"], ctx["node_emb"], TOP_K)
    return [t for t, _, _ in cands]


def rank_heuristic(question, model, ctx):
    result = search(
        question, model,
        ctx["node_ids"], ctx["index"], ctx["node_emb"], ctx["edge_emb"],
        ctx["tnm"], ctx["node_to_edges"], ctx["edge_to_nodes"],
        top_k=TOP_K, num_anchors=NUM_ANCHORS, max_hops=MAX_HOPS,
        alpha=ALPHA, beta=BETA, lambda_mmr=LAMBDA_MMR, diversity=0,
    )
    return [t for t, _ in sorted(result["table_deposits"].items(), key=lambda x: -x[1])]


def rank_trained(question, model, ctx):
    # TODO(phase 1b): load a scorer trained with JoinGR's beaver_/bird_train_eval_search_v13.py
    # (in-domain) or train_X_eval_Y_v01.py (BIRD -> Beaver) and rank with it.
    raise NotImplementedError


METHODS = {"vanilla": rank_vanilla, "heuristic": rank_heuristic, "trained": rank_trained}


def score(ranked, gold, scores):
    for k in KS:
        p, r = precision_at_k(ranked, gold, k), recall_at_k(ranked, gold, k)
        scores[k]["P"].append(p)
        scores[k]["R"].append(r)
        scores[k]["F1"].append(f1_at_k(p, r))
        scores[k]["PR"].append(perfect_recall_at_k(ranked, gold, k))


def average(scores, n):
    return {k: {m: sum(v) / n for m, v in s.items()} for k, s in scores.items()}


def evaluate(dataset, setting, encoder, method):
    g = load_graph(dataset, setting)
    node_ids, node_emb, index, edge_emb = load_embeddings(dataset, setting, encoder)
    ctx = {
        "node_ids": node_ids, "node_emb": node_emb, "index": index, "edge_emb": edge_emb,
        "tnm": g["table_node_map"], "node_to_edges": g["node_to_edges"], "edge_to_nodes": g["edge_to_nodes"],
    }
    model = SentenceTransformer(ENCODERS[encoder])
    rank = METHODS[method]

    # JoinGR's gold labels keep the SQL's casing, so e.g. 28% of Spider gold refs
    # ("pets_1.pets" vs graph "pets_1.Pets") can never match. "summary" follows the
    # paper's protocol; "summary_fixed" maps gold names onto the graph case-insensitively.
    canonical = {t.lower(): t for t in g["table_node_map"]["by_table"]}

    scores = {k: {"P": [], "R": [], "F1": [], "PR": []} for k in KS}
    scores_fixed = {k: {"P": [], "R": [], "F1": [], "PR": []} for k in KS}
    per_query = []
    start = time.perf_counter()
    for i, q in enumerate(g["queries"]):
        gold = {extract_table_name(t) for t in q["gold_tables"]}
        gold_fixed = {canonical.get(t.lower(), t) for t in gold}
        ranked = rank(q["question"], model, ctx)
        score(ranked, gold, scores)
        score(ranked, gold_fixed, scores_fixed)
        per_query.append({"i": i, "gold": sorted(gold_fixed), "ranked": ranked[:max(KS)], "n_ranked": len(ranked)})
        # TODO(task 3): column P/R/PR@k once gold_columns exist; rank columns by node score inside the retrieved tables.

    n = len(per_query)
    out = {
        "dataset": dataset, "setting": setting, "encoder": encoder, "method": method,
        "n_queries": n, "seconds": round(time.perf_counter() - start, 1),
        "summary": average(scores, n), "summary_fixed": average(scores_fixed, n), "per_query": per_query,
    }
    path = RESULTS / dataset / setting / encoder / f"{method}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
    return out, path


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", default="beaver")
    p.add_argument("--setting", default="documented")
    p.add_argument("--encoder", nargs="+", default=list(ENCODERS), choices=ENCODERS)
    p.add_argument("--method", nargs="+", default=["vanilla", "heuristic"], choices=METHODS)
    args = p.parse_args()

    for encoder in args.encoder:
        for method in args.method:
            out, path = evaluate(args.dataset, args.setting, encoder, method)
            s = out["summary"]
            print(f"{args.dataset}/{args.setting}/{encoder}/{method:<9} n={out['n_queries']}  "
                  + "  ".join(f"R@{k}={s[k]['R']:.3f} PR@{k}={s[k]['PR']:.3f}" for k in (5, 15))
                  + f"  PR@15(fixed gold)={out['summary_fixed'][15]['PR']:.3f}"
                  + f"  -> {path.relative_to(RESULTS.parent)}")
