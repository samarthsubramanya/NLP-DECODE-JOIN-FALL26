"""Sparsity-dependent thresholds (hp): Stage 2b cutoffs set per database from how sparse its data looks.

Fixed thresholds were chosen on clean data. Sparse data moves true joins toward them: missing parent rows lower
containment (G6 at 0.75, the orphan penalty past 20%), nulls cost null-term points. These rules measure that drift
on the database itself and move the cutoffs by the same amount. They read only the Stage 1 profile and the Stage 2a
keys, never gold, so they are legal inputs to the pipeline. No coefficient is fitted to a benchmark: each rule
moves a cutoff by the measured level itself (the band rule has one gain, reported with the results).

Signals (per database):
    orphan_level  how much containment real-looking joins have lost. For every source column take its best pair
                  into a chosen primary key whose distribution term does not flag it (d is None or d >= the cap),
                  then the 25th percentile of (1 - containment) over those sources, capped at 0.25. Clean data:
                  at least a quarter of such sources are real FKs at containment 1, so this stays ~0; deleting
                  parent rows lowers every real FK's containment and the level rises with it.
    null_level    median null share of the source columns that have a containment pair (candidate FK sources).
    thin_level    share of those sources with fewer than 5 distinct values (reported only, not used by a rule:
                  it is not 0 on clean data).

Rules (each one flag, `all` = every one):
    tau     containment_min = max(0.5, tau0 - orphan_level)            0.5 is where Stage 1 stops storing pairs
    orphan  orphan_max = orphan_max0 + orphan_level                    the x0.7 penalty starts later
    null    null_low, null_high += null_level (kept below 1)           the null term judges nulls relative to the db
    band    delta = delta0 * (1 + gain * max(orphan, null))            sparser data sends more to the LLM, auto-accepts less
At level 0 every rule leaves the configuration unchanged, so clean results are identical to the fixed pipeline.
"""
import statistics

RULES = ("tau", "orphan", "null", "band")
ORPHAN_CAP, TAU_FLOOR, THIN_DISTINCT = 0.25, 0.5, 5
BAND_GAIN = 2.0


def _quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return 0.0
    i = q * (len(xs) - 1)
    lo, hi = int(i), min(int(i) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def signals(profile, keys, cfg):
    tables = profile["tables"]
    pks = {(t, k["primary_key"]) for t, k in keys["tables"].items() if k.get("primary_key")}
    cap = cfg.get("dist_no_accept_below")
    best, sources = {}, set()
    for p in profile.get("containment", []):
        src, tgt = tuple(p["source"]), tuple(p["target"])
        if src[0] == tgt[0]:
            continue
        sources.add(src)
        d = p.get("dist")
        if tgt in pks and (d is None or cap is None or d >= cap):
            best[src] = max(best.get(src, 0.0), p["containment"])
    orphan = min(ORPHAN_CAP, _quantile([1 - c for c in best.values()], 0.25)) if best else 0.0
    nulls = [tables[t]["columns"][c]["null_rate"] for t, c in sources]
    thin = [tables[t]["columns"][c]["distinct"] < THIN_DISTINCT for t, c in sources]
    return {"orphan_level": round(orphan, 4), "null_level": round(statistics.median(nulls), 4) if nulls else 0.0,
            "thin_level": round(sum(thin) / len(thin), 4) if thin else 0.0, "sources": len(sources),
            "anchored_sources": len(best)}


def adapt(cfg, sig, rules, gain=BAND_GAIN):
    """A copy of the Stage 2b configuration with the chosen rules applied."""
    rules = RULES if "all" in rules else tuple(rules)
    out = dict(cfg)
    o, n = sig["orphan_level"], sig["null_level"]
    if "tau" in rules:
        out["containment_min"] = max(TAU_FLOOR, cfg["containment_min"] - o)
    if "orphan" in rules:
        out["orphan_max"] = cfg["orphan_max"] + o
    if "null" in rules:
        out["null_low"] = min(0.95, cfg["null_low"] + n)
        out["null_high"] = min(0.99, max(out["null_low"], cfg["null_high"] + n))
    if "band" in rules:
        out["delta"] = cfg["delta"] * (1 + gain * max(o, n))
    out["adaptive_rules"] = list(rules)
    out["sparsity_signals"] = sig
    return out
