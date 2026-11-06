"""Scores JoinGR baselines on one dataset/setting/encoder and writes results JSON.

Tables are ranked by the method. Columns are ranked inside the method's top-15
tables by query-column similarity, so a missed table also hides its columns.
Gold columns come from gold/<dataset>.json (baselines.gold_columns) as node ids,
which stay the same in every setting, obfuscated included.

vanilla    dense retrieval: tables ranked by their best-matching column
heuristic  JoinGR's untrained walk with the repo's default settings
trained    JoinGR's learned scorer, frozen; optional (see rank_trained)
merged     heuristic, keeping 2 of the top 15 for the best dense tables it missed
"""

import argparse
import json
import time

import numpy as np

from sentence_transformers import SentenceTransformer

from baselines.joingr import (
    ENCODERS, RESULTS, TOP_K, NUM_ANCHORS, MAX_HOPS, ALPHA, BETA, LAMBDA_MMR,
    ROOT, embed_query, search, search_anchor_candidates, extract_table_name,
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
    raise NotImplementedError


def rank_merged(question, model, ctx, budget=15, dense_slots=2):
    """The walk's top tables, with the last dense_slots of the budget going to the best dense tables it missed."""
    walk, dense = rank_heuristic(question, model, ctx), rank_vanilla(question, model, ctx)
    head = walk[:budget - dense_slots]
    extra = [t for t in dense if t not in head][:dense_slots]
    return head + extra + [t for t in walk + dense if t not in head and t not in extra]


METHODS = {"vanilla": rank_vanilla, "heuristic": rank_heuristic, "trained": rank_trained, "merged": rank_merged}


def rank_columns(query_vec, tables, ctx, table_budget=15):
    # TODO(phase 3): DECODE-JOIN may re-rank columns with its descriptions and join roles;
    # this baseline only uses the column embeddings JoinGR already has.
    by_table = ctx["tnm"]["by_table"]
    cols = [nid for t in tables[:table_budget] for nid in by_table.get(t, {}).get("all_nodes", [])]
    if not cols:
        return []
    sims = np.stack([ctx["node_emb"][n] for n in cols]) @ query_vec
    return [cols[j] for j in np.argsort(-sims)]


def load_gold_columns(dataset):
    path = ROOT / "gold" / f"{dataset}.json"
    if not path.exists():
        return None
    return {x["i"]: set(x["gold_columns"]) for x in json.loads(path.read_text())}


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
    gold_columns = load_gold_columns(dataset)

    scores = {k: {"P": [], "R": [], "F1": [], "PR": []} for k in KS}
    scores_fixed = {k: {"P": [], "R": [], "F1": [], "PR": []} for k in KS}
    scores_cols = {k: {"P": [], "R": [], "F1": [], "PR": []} for k in KS}
    per_query = []
    start = time.perf_counter()
    for i, q in enumerate(g["queries"]):
        gold = {extract_table_name(t) for t in q["gold_tables"]}
        gold_fixed = {canonical.get(t.lower(), t) for t in gold}
        ranked = rank(q["question"], model, ctx)
        score(ranked, gold, scores)
        score(ranked, gold_fixed, scores_fixed)
        row = {"i": i, "gold": sorted(gold_fixed), "ranked": ranked[:max(KS)], "n_ranked": len(ranked)}

        # Questions with no gold columns (SELECT count(*), unparsable SQL) are left out of column metrics.
        # A query subset (e.g. the PoC's filtered questions) carries its index in the full gold file.
        gi = q.get("gold_index", i)
        if gold_columns and gold_columns.get(gi):
            cols = rank_columns(embed_query(q["question"], model), ranked, ctx)
            score(cols, gold_columns[gi], scores_cols)
            row["ranked_columns"] = cols[:max(KS)]
        per_query.append(row)

    n = len(per_query)
    out = {
        "dataset": dataset, "setting": setting, "encoder": encoder, "method": method,
        "n_queries": n, "seconds": round(time.perf_counter() - start, 1),
        "summary": average(scores, n), "summary_fixed": average(scores_fixed, n),
        "n_column_queries": len(scores_cols[KS[0]]["R"]),
        "summary_columns": average(scores_cols, len(scores_cols[KS[0]]["R"])) if scores_cols[KS[0]]["R"] else None,
        "per_query": per_query,
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
                  + (f"  colR@15={out['summary_columns'][15]['R']:.3f} colPR@15={out['summary_columns'][15]['PR']:.3f}"
                     if out["summary_columns"] else "")
                  + f"  -> {path.relative_to(RESULTS.parent)}")
