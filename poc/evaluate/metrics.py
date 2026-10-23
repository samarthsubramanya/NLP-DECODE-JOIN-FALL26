"""Evaluation metrics, one function each, picked by name in config [evaluate].metrics.

To add a metric: write `def my_metric(ctx) -> dict`, decorate it with @metric("name"),
and list "name" in the config. ctx gives the run's outputs per database, the gold
keys in the setting's names, name translation, and the config.
"""

import json
import re
from collections import Counter
from functools import cached_property

from poc.evaluate.gold import Names, gold_keys, load_mapping, reference_descriptions

METRICS = {}


def metric(name):
    def register(fn):
        METRICS[name] = fn
        return fn
    return register


class Context:
    def __init__(self, cfg, setting, run_path, dbs):
        self.cfg, self.setting, self.run_path, self.dbs = cfg, setting, run_path, dbs
        self.names = Names(load_mapping(cfg, setting))

    @cached_property
    def gold(self):
        return gold_keys(self.cfg, self.names)

    def gold_for(self, db):
        return self.gold.get(db.lower(), {"pks": set(), "composite": 0, "fks": set()})


def prf(pred, gold):
    tp = len(pred & gold)
    p = tp / len(pred) if pred else 0.0
    r = tp / len(gold) if gold else 0.0
    return {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.0, "tp": tp, "pred": len(pred), "gold": len(gold)}


def _col(x):
    return (x["table"].lower(), x["column"].lower())


def _pair(a, b):
    return frozenset((a, b))


@metric("pk")
def primary_keys(ctx):
    pred, gold, composite = set(), set(), 0
    for db, out in ctx.dbs.items():
        g = ctx.gold_for(db)
        gold |= {(db, *x) for x in g["pks"]}
        composite += g["composite"]
        pred |= {(db, t.lower(), k["primary_key"].lower()) for t, k in out["keys"]["tables"].items() if k["primary_key"]}
    return {**prf(pred, gold), "composite_pks_skipped": composite}


@metric("fk")
def foreign_keys(ctx):
    """Join graph vs declared FKs, per graph (stats, llm). Undirected, plus how many got the direction right."""
    result = {}
    for graph in ("stats", "llm"):
        if not all(graph in out["joins"]["graphs"] for out in ctx.dbs.values()):
            continue
        pred, gold, directed, per_db = set(), set(), 0, {}
        for db, out in ctx.dbs.items():
            g = {(a, b) for a, b in ctx.gold_for(db)["fks"]}
            gold_db = {_pair((db, *a), (db, *b)) for a, b in g}
            edges = out["joins"]["graphs"][graph]
            pred_db = {_pair((db, *_col(e["source"])), (db, *_col(e["target"]))) for e in edges}
            directed += sum((_col(e["source"]), _col(e["target"])) in g for e in edges)
            per_db[db] = prf(pred_db, gold_db)
            pred |= pred_db
            gold |= gold_db
        result[graph] = {**prf(pred, gold), "direction_correct": directed, "per_db": per_db}
    return result


@metric("fk_by_decision")
def fk_by_decision(ctx):
    """How many candidates each decision produced, and how many of them are declared FKs."""
    counts = Counter()
    for db, out in ctx.dbs.items():
        gold = {_pair(a, b) for a, b in ctx.gold_for(db)["fks"]}
        for r in out["joins"]["candidates"]:
            counts[(r["decision"], _pair(_col(r["source"]), _col(r["target"])) in gold)] += 1
    decisions = sorted({d for d, _ in counts})
    return {d: {"n": counts[(d, True)] + counts[(d, False)], "in_gold": counts[(d, True)]} for d in decisions}


@metric("llm_usage")
def llm_usage(ctx):
    path = ctx.run_path / "llm_calls.jsonl"
    if not path.exists():
        return {}
    calls = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    by = Counter((c["stage"], c["source"]) for c in calls)
    tokens = sum((c.get("usage") or {}).get("total_tokens", 0) for c in calls)
    return {
        "calls": {f"{s}/{src}": n for (s, src), n in sorted(by.items())},
        "unparsable": sum(not c["ok"] for c in calls),
        "api_tokens": tokens,
    }


@metric("description_status")
def description_status(ctx):
    status, roles = Counter(), Counter()
    for out in ctx.dbs.values():
        for t in (out.get("descriptions") or {}).get("tables", {}).values():
            for c in t["columns"].values():
                status[c["status"]] += 1
                roles[c["role"] or "-"] += 1
    total = sum(status.values())
    return {"columns": total, "status": {k: v / total for k, v in status.items()} if total else {}, "roles": dict(roles)}


def _tokens(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def token_f1(a, b):
    ta, tb = _tokens(a), _tokens(b)
    tp = len(ta & tb)
    return 2 * tp / (len(ta) + len(tb)) if ta and tb else 0.0


@metric("description_reference")
def description_reference(ctx):
    """Generated name + description vs the dataset's own column documentation.
    Token F1 always; embedding cosine when [evaluate].description_encoder is set."""
    pairs = []
    for db, out in ctx.dbs.items():
        refs = reference_descriptions(ctx.cfg, db)
        if not refs:
            continue
        for table, t in (out.get("descriptions") or {}).get("tables", {}).items():
            for col, c in t["columns"].items():
                _, otable, ocol = ctx.names.to_original(db, table, col)
                ref = refs.get((otable, ocol))
                if ref:
                    pairs.append((f"{c['name']} {c['description']}".strip(), ref, c["status"]))
    if not pairs:
        return {"note": "no reference descriptions found"}

    scores = {"token_f1": [token_f1(g, r) for g, r, _ in pairs]}
    encoder = ctx.cfg["evaluate"].get("description_encoder")
    if encoder:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(encoder)
        ga = model.encode([g or " " for g, _, _ in pairs], normalize_embeddings=True)
        ra = model.encode([r for _, r, _ in pairs], normalize_embeddings=True)
        scores["cosine"] = [float(x @ y) for x, y in zip(ga, ra)]

    out = {"columns_with_reference": len(pairs)}
    for name, vals in scores.items():
        out[name] = sum(vals) / len(vals)
        out[f"{name}_by_status"] = {
            s: sum(v for v, (_, _, st) in zip(vals, pairs) if st == s) / n
            for s, n in Counter(st for _, _, st in pairs).items()
        }
    return out
