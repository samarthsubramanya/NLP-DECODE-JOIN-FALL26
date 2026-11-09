"""Stage 2b-c: foreign keys from data, then a greedy join graph. No LLM.

Source a -> target b is a candidate when b is a candidate key of another table and
the value families match. Containment I(a->b) = |Va ∩ Vb| / |Va| is exact (full
columns). Hard gates run first, then the weighted score (points out of 100).
Coverage, the share of the target's values the source uses, is an optional extra
signal (weight 0 = the proposal as written): it separates a real FK from small
integers that happen to fall inside a key's range.
Records just around the cutoff (the delta band) are left for the LLM; everything
is kept with its components so any edge can be traced back.
"""

import re

from poc.catalog import q

CAST = {"int": "TRY_CAST({c} AS BIGINT)", "float": "TRY_CAST({c} AS DOUBLE)", "date": "TRY_CAST({c} AS TIMESTAMP)", "text": "CAST({c} AS VARCHAR)"}
NUMERIC = {"int", "float"}
NEVER_SOURCE_ROLES = {"boolean", "free_text", "float"}


def compatible(fa, fb):
    return fa == fb or {fa, fb} <= NUMERIC


def cast_expr(col, fa, fb):
    family = "float" if fa != fb else fa
    return CAST.get(family, CAST["text"]).format(c=q(col))


def containment(cat, ta, a, tb, b, fa, fb):
    ea, eb = cast_expr(a, fa, fb), cast_expr(b, fa, fb)
    n_a, hit = cat.scalar(
        f"WITH va AS (SELECT DISTINCT {ea} AS v FROM {q(ta)} WHERE {ea} IS NOT NULL), "
        f"vb AS (SELECT DISTINCT {eb} AS v FROM {q(tb)} WHERE {eb} IS NOT NULL) "
        f"SELECT count(*), count(vb.v) FROM va LEFT JOIN vb ON va.v = vb.v"
    )
    return (hit / n_a if n_a else 0.0), hit


def tokens(name):
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return {t for t in re.split(r"[^a-zA-Z0-9]+", name.lower()) if t}


def name_similarity(a, b, b_table):
    if a.lower() == b.lower():
        return 1.0
    ta, tb = tokens(a), tokens(b) | tokens(b_table)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def prefilter(pa, pb, cfg):
    if pa["role"] in NEVER_SOURCE_ROLES or pa["distinct"] < cfg["joins"]["min_source_distinct"]:
        return False
    if not compatible(pa["family"], pb["family"]):
        return False
    if pa["distinct"] * cfg["joins"]["containment_min"] > pb["distinct"]:
        return False
    na, nb = pa.get("numeric"), pb.get("numeric")
    if na and nb and na["min"] is not None and nb["min"] is not None and (na["max"] < nb["min"] or na["min"] > nb["max"]):
        return False
    return True


def key_source_allowed(source_is_pk, target_is_pk, mode):
    if not source_is_pk or mode == "always":
        return True
    return mode == "pk_to_pk" and target_is_pk


def weights(cfg, use_names):
    w = dict(cfg["joins"]["weights"])
    if not use_names:
        w["overlap"] += w.pop("name")
    return w


def decide(score, cfg):
    accept, delta, band = cfg["joins"]["accept"], cfg["adjudicate"]["delta"], cfg["adjudicate"]["band"]
    lo = accept - delta if band in ("below", "both") else accept
    hi = accept + delta if band in ("above", "both") else accept
    if lo <= score < hi:
        return "band"
    return "accepted" if score >= accept else "rejected_score"


def run(cat, profiles, keys, cfg, log=print):
    use_names = keys["use_names"]
    w = weights(cfg, use_names)
    tau, mode = cfg["joins"]["containment_min"], cfg["joins"]["key_sources"]
    tables = profiles["tables"]

    records, skipped = [], 0
    for tb, kb in keys["tables"].items():
        for cand in kb["candidate_keys"]:
            b = cand["column"]
            pb = tables[tb]["columns"][b]
            target_is_pk = kb["primary_key"] == b
            for ta, t in tables.items():
                if ta == tb:
                    continue
                for a, pa in t["columns"].items():
                    if not prefilter(pa, pb, cfg):
                        skipped += 1
                        continue
                    rec = {
                        "source": {"table": ta, "column": a},
                        "target": {"table": tb, "column": b},
                        "target_is_pk": target_is_pk,
                        "source_is_pk": keys["tables"][ta]["primary_key"] == a,
                    }
                    if not key_source_allowed(rec["source_is_pk"], target_is_pk, mode):
                        records.append({**rec, "decision": "rejected_gate", "reason": f"key source ({mode})"})
                        continue
                    c, hit = containment(cat, ta, a, tb, b, pa["family"], pb["family"])
                    rec["containment"] = c
                    rec["coverage"] = hit / pb["distinct"] if pb["distinct"] else 0.0
                    if c < tau:
                        records.append({**rec, "decision": "rejected_gate", "reason": f"containment {c:.2f} < {tau}"})
                        continue
                    if rec["coverage"] < cfg["joins"]["coverage_min"]:
                        records.append({**rec, "decision": "rejected_gate", "reason": f"coverage {rec['coverage']:.2f} < {cfg['joins']['coverage_min']}"})
                        continue
                    parts = {
                        "overlap": c,
                        "coverage": rec["coverage"],
                        "cardinality": 1.0 if pa["uniqueness"] < 1.0 else 0.5,
                        "target_key": 1.0 if target_is_pk else 0.5,
                        "nulls": 1.0 - pa["null_rate"],
                    }
                    if use_names:
                        parts["name"] = name_similarity(a, b, tb)
                    rec["cardinality"] = "many_to_one" if pa["uniqueness"] < 1.0 else "one_to_one"
                    rec["parts"] = parts
                    rec["score"] = sum(w[k] * v for k, v in parts.items())
                    rec["decision"] = decide(rec["score"], cfg)
                    records.append(rec)

    _cap_targets(records, cfg["joins"]["max_targets"])
    result = {"db_id": profiles["db_id"], "use_names": use_names, "skipped_prefilter": skipped, "candidates": records}
    accept = cfg["joins"]["accept"]
    result["graphs"] = {"stats": assemble(records, {"accepted"}, cfg, extra=lambda r: r["decision"] == "band" and r["score"] >= accept)}
    n_band = sum(r["decision"] == "band" for r in records)
    log(f"  joins: {len(records)} candidates scored, {len(result['graphs']['stats'])} edges (stats), {n_band} in the LLM band")
    return result


def _cap_targets(records, max_targets):
    # Each source keeps at most max_targets surviving targets; the rest are gated out.
    by_source = {}
    for r in records:
        if r["decision"] in ("accepted", "band"):
            by_source.setdefault((r["source"]["table"], r["source"]["column"]), []).append(r)
    for recs in by_source.values():
        for r in sorted(recs, key=lambda r: -r["score"])[max_targets:]:
            r["decision"], r["reason"] = "rejected_gate", f"more than {max_targets} targets"


def assemble(records, include, cfg, extra=None):
    """Greedy, HoPF-style: highest score first, one edge per column pair, max_targets per source."""
    chosen = [r for r in records if r["decision"] in include or (extra and extra(r))]
    edges, pairs, per_source = [], set(), {}
    for r in sorted(chosen, key=lambda r: -r["score"]):
        src = (r["source"]["table"], r["source"]["column"])
        dst = (r["target"]["table"], r["target"]["column"])
        pair = frozenset((src, dst))
        if pair in pairs or per_source.get(src, 0) >= cfg["joins"]["max_targets"]:
            continue
        pairs.add(pair)
        per_source[src] = per_source.get(src, 0) + 1
        edges.append({k: r[k] for k in ("source", "target", "containment", "coverage", "cardinality", "score", "decision")})
    return edges
