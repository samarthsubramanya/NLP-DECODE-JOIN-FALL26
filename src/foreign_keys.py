"""
Stage 2b: foreign keys and the join graph from the Stage 1 profiles and the Stage 2a keys.
No LLM, no declared keys, no gold SQL, no database access: only out/profiles and out/keys.

DBAutoDoc foreign-key scoring on whole columns. The tags say where each rule comes from:
[Paper] DBAutoDoc, [Report] our proposal, [Team] the formulas written down for the poc config,
[Ours] an interpretation or an addition.

Candidates are the Stage 1 containment pairs (exact over the full columns, so no 500-value
sample): source a -> target b, with I(a->b) = |Va ∩ Vb| / |Va|.

Gates and prefilters, in this order; the first one that fires rejects the pair ("rejected_gate"):
    same table       excluded unless --self-refs                                      [Ours, answered]
    G8               a table's chosen primary key is never a source                   [Paper, Report]
                     unless it is a sparse integer key: density (distinct / (max - min + 1))
                     at most 0.5. Subclass tables reuse their parent's key (FIBEN: a
                     corporation is an agent), drawing scattered values from one shared ID
                     pool; dense 1..n keys overlap other 1..n keys by chance, so they stay
                     gated. pk_source_max_density = None keeps G8 absolute                     [Ours]
    G1               the target must have survived 2a's hard rejections; it need not be
                     the chosen key, so k can be 0                                    [Paper, answered]
    G3               a column named rowguid is never a target (only when names are used)   [Paper]
    prefilters       source role boolean / free text / float; fewer than 2 distinct values;
                     average length over 64 characters                                [Paper tier 2, Team]
    G6               containment >= 0.75, exact                                       [Paper, Report]
    G9               integer pairs: the source's range (max - min + 1) must span at least
                     min_range_span of the target's. A real foreign key draws from the whole
                     parent key; a measure (a 0-100 rating, a 1-5 count) sits in a corner of a
                     1..n id and is contained by chance. OFF by default (None) since the soft
                     distribution term d replaced it; --min-range-span 0.1 turns it on      [Ours]

Score of a candidate that passes (points out of 100), then the penalties:
    s_FK = 25·v + 15·d + 20·s + 15·r + 15·k + 10·nu
      v  containment I(a->b)
      d  distribution: 1 - EMD/EMD_max of where the source's matching values fall over L (<= 10)
         quantile buckets of the target's sort order (Stage 1 pair["dist"]); 1 = spread over the whole
         target range, 0 = all in one end. A small-integer measure sitting in a corner of an id range is
         contained by chance but scores ~0. None (target with fewer than 5 distinct values, no hit, or an old profile) = no evidence:
         that record's d points go to v                                                          [Ours]
      s  name similarity: best of source name vs b, "{b_table}_{b}", "{b_table}_id" (lower-case,
         letters and digits only): 1.0 equal, 0.8 one contains the other, else 1 - Levenshtein/longest
      r  min(rho, 2) / 2, rho = rows(source table) / distinct(source column)
      k  1 if b is its table's chosen primary key (2a), else 0
      nu null share of the source: below 30% 1.0, below 70% 0.5, else 0
    no names (obfuscated):  40·v + 20·d + 15·r + 15·k + 10·nu: the old 60 overlap points (the 40 plus the
                            20 name points [Report]) are split into containment and distribution
    adaptive:               if more than 40% of the candidates that passed the gates have k = 0,
                            the 15 points of k go to overlap (25 -> 40)       [Paper; scope: one database]
    x 0.7  orphan rate 1 - v above 0.2                                                       [Paper]
    x 0.5  incompatible types; inert here, Stage 1 pairs are always the same family          [Paper]
    G4     source table far smaller than the target: the paper gives no multiplier, so it is a
           knob that is OFF (g4_ratio / g4_multiplier)                                       [Paper, answered]
    G5     each source keeps its 3 best targets, the rest are rejected                       [Paper]
    x psi(n)  n = targets kept for the source after G5: 1.0 / 0.85 / 0.75 / 0.65 for 1 / 2 / 3 / 4+
                                                                                   [Paper; counting answered]
    cap    a record whose distribution term d is below dist_no_accept_below (0.3) has its final
           score (after the penalties, G5 and fan-out) capped at accept + delta - 1 (69), so it
           lands in the LLM band and is never auto-accepted. A lower score is not raised; d = None
           (no evidence) is not capped; "dist_capped": true marks a score the cap changed. 0.3 is
           the 5th percentile of d for 3 source values spread uniformly at random over 10 buckets
           (about 0.44 at 5 values, 0.77 at 30); it is not fit to any dataset.             [Ours]

Decision on the final score (cutoff = accept, band width = delta):
    below 60        rejected_score
    60 to below 70  band: the weakest accepted records; Stage 3's LLM accepts, rejects or abstains
    70 and above    accepted

Graph, built greedily like HoPF [Report]: highest score first, one edge per column pair
(the graph is undirected), at most 3 edges per source. Cycles are kept.
    graphs.direct   accepted records only (the base the LLM's accepts are added to)
    graphs.stats    accepted + band records: what statistics alone would export (RQ1, variant 2)
Stage 3 adds graphs.llm = accepted + llm_accept. Every candidate keeps its score parts, weights,
penalties and the target's 2a score, so any edge, and the LLM prompt for a band record, can be traced.

Repair (--repair-isolated K, off by default) [Ours]: a table with rows and no edge in graphs.stats gets up to K
extra band records from out/overlaps, one per partner table, ranked by name similarity (when names are used), then
share and number of shared values. Integer pairs need both columns sparse (density at most pk_source_max_density),
since dense ids overlap by chance. These records skip the gates and the score (it is fixed mid-band), so Stage 3's
LLM decides each one; "partial_overlap": true marks them.

Choices the sources leave open (all in DEFAULTS):
  - the name comparison uses the same lower-case, letters-and-digits form as Stage 2a;
  - when names are off and the adaptive shift fires, both moves go to overlap (40 + 20 + 15);
  - the email/URL prefilter of the paper's tier 2 is not applied: it needs a full-column share
    that Stage 1 does not compute yet (only the average-length rule is).

Usage:
    python src/foreign_keys.py --dataset bird
    python src/foreign_keys.py --dataset bird --databases financial --run try
    python src/foreign_keys.py --dataset bird --keys-run no_names --run no_names   # obfuscated simulation

Input:  out/profiles/<dataset>/<db_id>.json   (src/profile_columns.py; made here if missing)
        out/keys/<dataset>/<keys-run>/<db_id>.json   (src/primary_keys.py or evals/eval_primary_keys.py)
Output: out/joins/<dataset>/<run>/<db_id>.json
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

from primary_keys import ROOT, load_profile, norm, work_list

DEFAULTS = {
    "accept": 60,                     # cutoff on the final score
    "delta": 10,                      # band width: [accept, accept + delta) goes to the LLM
    "containment_min": 0.75,          # G6, tau
    "max_targets": 3,                 # G5
    "weights": None,                  # None: W_NAMED / W_NO_NAMES by setting; a dict overrides both
    "adaptive_k_zero_share": 0.4,     # more candidates than this with k = 0 -> k's points go to overlap
    "r_cap": 2,
    "null_low": 0.3,
    "null_high": 0.7,
    "name_contains": 0.8,
    "orphan_max": 0.2,
    "orphan_penalty": 0.7,
    "incompatible_penalty": 0.5,
    "fan_out": [1.0, 0.85, 0.75, 0.65],   # psi for 1, 2, 3, 4+ kept targets
    "min_source_distinct": None,      # prefilter off (null_check); 2 = the old "fewer than 2 distinct" rule
    "long_text_len": 64,
    "never_source_roles": ["boolean", "free_text", "float"],
    "self_refs": False,
    "pk_source_max_density": None,    # G8 absolute (null_check); 0.5 spares sparse int primary keys
    "min_range_span": None,           # G9, int pairs; None: off (the soft distribution term replaced it)
    "dist_no_accept_below": 0.3,      # d below this: final score capped at accept + delta - 1; None: no cap
    "g4_ratio": None,                 # G4 off: source rows < ratio * target rows ...
    "g4_multiplier": None,            # ... multiplies the score by this
    "repair_isolated": 0,             # partner tables per isolated table; 0: off
    "g1": False,                      # G1 off (null_check): targets rejected by 2a may still be targets
    "keys_run": "frozen",
}

# points out of 100; overlap = containment, distribution = where the source's values sit in the target's range
W_NAMED = {"overlap": 25, "distribution": 15, "name": 20, "cardinality": 15, "target_key": 15, "nulls": 10}
W_NO_NAMES = {"overlap": 40, "distribution": 20, "name": 0, "cardinality": 15, "target_key": 15, "nulls": 10}

ORDER = {"accepted": 0, "band": 1, "rejected_score": 2, "rejected_gate": 3}


# ------------------------------------------------------------------ scoring pieces
def levenshtein(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def name_similarity(source_col, target_table, target_col, cfg):
    """(similarity, which target string matched best). Lower-case letters and digits only."""
    a = norm(source_col)
    best, via = 0.0, None
    for label, text in (("column", target_col), ("table_column", f"{target_table}_{target_col}"),
                        ("table_id", f"{target_table}_id")):
        b = norm(text)
        if not a or not b:
            continue
        if a == b:
            s = 1.0
        elif a in b or b in a:
            s = cfg["name_contains"]
        else:
            s = 1 - levenshtein(a, b) / max(len(a), len(b))
        if s > best:
            best, via = s, label
    return best, via


def null_term(rate, cfg):
    return 1.0 if rate < cfg["null_low"] else 0.5 if rate < cfg["null_high"] else 0.0


def fan_out_factor(n, cfg):
    return cfg["fan_out"][min(n, len(cfg["fan_out"])) - 1]


def weights(cfg, use_names, adaptive):
    w = dict(cfg.get("weights") or (W_NAMED if use_names else W_NO_NAMES))
    if adaptive:
        w["overlap"] += w["target_key"]
        w["target_key"] = 0
    return w


# ------------------------------------------------------------------ candidates
def sparse_key(p, cfg):
    """An integer key whose values are scattered over its range (a shared ID pool), not numbered 1..n."""
    density = (p.get("numeric") or {}).get("density")
    return cfg["pk_source_max_density"] is not None and p["family"] == "int" and density is not None \
        and density <= cfg["pk_source_max_density"]


def range_span(a, b):
    """(source max - min + 1) / (target max - min + 1) for two integer columns, else None."""
    na, nb = a.get("numeric") or {}, b.get("numeric") or {}
    if a["family"] != "int" or b["family"] != "int" or None in (na.get("min"), na.get("max"), nb.get("min"), nb.get("max")):
        return None
    return (na["max"] - na["min"] + 1) / (nb["max"] - nb["min"] + 1)


def gate(pair, profile, keys, cfg, use_names):
    """Reason the pair is rejected by a gate or prefilter, or None. Also returns the profiles."""
    (st, sc), (tt, tc) = pair["source"], pair["target"]
    tables = profile["tables"]
    a, b = tables[st]["columns"][sc], tables[tt]["columns"][tc]
    if st == tt and not cfg["self_refs"]:
        return "same table"
    if keys["tables"][st]["primary_key"] == sc and not sparse_key(a, cfg):
        return "G8: the source is its table's dense primary key"
    rejected = keys["tables"][tt]["columns"][tc].get("rejected")
    if rejected and cfg["g1"]:
        return f"G1: target not eligible ({rejected})"
    if use_names and tc.lower() == "rowguid":
        return "G3: rowguid target"
    if a["role"] in cfg["never_source_roles"]:
        return f"prefilter: source role {a['role']}"
    if cfg["min_source_distinct"] is not None and a["distinct"] < cfg["min_source_distinct"]:
        return "prefilter: fewer than 2 distinct values"
    if (a.get("text") or {}).get("avg_len", 0) > cfg["long_text_len"]:
        return f"prefilter: average length over {cfg['long_text_len']}"
    if pair["containment"] < cfg["containment_min"]:
        return f"G6: containment {pair['containment']:.3f} < {cfg['containment_min']}"
    span = range_span(a, b) if cfg["min_range_span"] is not None else None
    if span is not None and span < cfg["min_range_span"]:
        return f"G9: source range spans {span:.3f} of the target's < {cfg['min_range_span']}"
    return None


def make_record(pair, profile, keys, use_names):
    (st, sc), (tt, tc) = pair["source"], pair["target"]
    a = profile["tables"][st]["columns"][sc]
    target_key = keys["tables"][tt]["columns"][tc]
    return {
        "source": {"table": st, "column": sc},
        "target": {"table": tt, "column": tc},
        "target_is_pk": keys["tables"][tt]["primary_key"] == tc,
        "target_pk_score": target_key.get("score"),          # Stage 2a score, for the LLM prompt
        "containment": pair["containment"],
        "coverage": pair["coverage"],
        "cardinality": "one_to_one" if pair["source_unique"] else "many_to_one",
        "dist": pair.get("dist"),                            # Stage 1 quantile-EMD, None = no evidence
        "dist_buckets": pair.get("dist_buckets"),            # source hits per target quantile bucket
    }


def score_candidates(records, profile, cfg, use_names):
    """Fills parts, weights, penalties and a score on every record that passed the gates."""
    tables = profile["tables"]
    k_zero = sum(not r["target_is_pk"] for r in records) / len(records) if records else 0.0
    adaptive = k_zero > cfg["adaptive_k_zero_share"]
    w = weights(cfg, use_names, adaptive)
    for r in records:
        st, sc, tt = r["source"]["table"], r["source"]["column"], r["target"]["table"]
        a = tables[st]["columns"][sc]
        rho = tables[st]["rows"] / a["distinct"]
        parts = {
            "overlap": r["containment"],
            "distribution": r["dist"],
            "cardinality": min(rho, cfg["r_cap"]) / cfg["r_cap"],
            "target_key": 1.0 if r["target_is_pk"] else 0.0,
            "nulls": null_term(a["null_rate"], cfg),
        }
        if use_names:
            parts["name"], r["name_via"] = name_similarity(sc, tt, r["target"]["column"], cfg)
        r["parts"] = {k: None if v is None else round(v, 4) for k, v in parts.items()}
        wr = dict(w)                       # no distribution evidence: this record's points go to overlap
        if parts["distribution"] is None:
            wr["overlap"] += wr["distribution"]
            wr["distribution"] = 0
        r["weights_used"] = wr
        raw = sum(wr[k] * v for k, v in parts.items() if v is not None)
        penalties = {}
        if 1 - r["containment"] > cfg["orphan_max"]:
            penalties["orphan"] = cfg["orphan_penalty"]
        if a["family"] != tables[tt]["columns"][r["target"]["column"]]["family"]:
            penalties["incompatible_types"] = cfg["incompatible_penalty"]
        if cfg["g4_ratio"] is not None and cfg["g4_multiplier"] is not None:
            if tables[st]["rows"] < cfg["g4_ratio"] * tables[tt]["rows"]:
                penalties["g4_row_ratio"] = cfg["g4_multiplier"]
        score = raw
        for m in penalties.values():
            score *= m
        r["score_before_fan_out"] = round(score, 4)
        r["penalties"] = penalties
    return w, adaptive, k_zero


def cap_and_fan_out(records, cfg):
    """G5 (best max_targets per source), then psi(n); the rest are rejected_gate."""
    by_source = defaultdict(list)
    for r in records:
        by_source[(r["source"]["table"], r["source"]["column"])].append(r)
    for recs in by_source.values():
        recs.sort(key=lambda r: (-r["score_before_fan_out"], r["target"]["table"], r["target"]["column"]))
        for r in recs[cfg["max_targets"]:]:
            r["decision"], r["reason"] = "rejected_gate", f"G5: more than {cfg['max_targets']} targets"
        kept = recs[:cfg["max_targets"]]
        psi = fan_out_factor(len(kept), cfg)
        for r in kept:
            r["fan_out"] = psi
            r["score"] = round(r["score_before_fan_out"] * psi, 4)


def decide(score, cfg):
    if score < cfg["accept"]:
        return "rejected_score"
    return "band" if score < cfg["accept"] + cfg["delta"] else "accepted"


def cap_low_distribution(records, cfg):
    """A record whose source sits in a corner of the target's range (distribution term below the threshold)
    is never auto-accepted: its final score is capped just under the band's top. A lower score is kept."""
    limit, top = cfg["dist_no_accept_below"], cfg["accept"] + cfg["delta"] - 1
    if limit is None:
        return
    for r in records:
        d = r["parts"]["distribution"] if "score" in r else None
        if d is not None and d < limit and r["score"] > top:
            r["score"], r["dist_capped"] = top, True


def assemble(records, include, cfg):
    """Greedy, HoPF-style: highest score first, one edge per column pair, max_targets per source."""
    chosen = [r for r in records if r["decision"] in include]
    edges, pairs, per_source = [], set(), defaultdict(int)
    for r in sorted(chosen, key=lambda r: (-r["score"], r["source"]["table"], r["source"]["column"],
                                           r["target"]["table"], r["target"]["column"])):
        src = (r["source"]["table"], r["source"]["column"])
        dst = (r["target"]["table"], r["target"]["column"])
        pair = frozenset((src, dst))
        if pair in pairs or per_source[src] >= cfg["max_targets"]:
            continue
        pairs.add(pair)
        per_source[src] += 1
        edges.append({k: r[k] for k in ("source", "target", "containment", "coverage", "cardinality", "score", "decision")})
    return edges


def repair_records(graph, profile, keys, overlaps, cfg, use_names):
    """Band records for the tables with rows that graph leaves without a join (see the module docstring)."""
    tables = profile["tables"]
    joined = {e[side]["table"] for e in graph for side in ("source", "target")}
    isolated = {t for t, tp in tables.items() if tp["rows"] and t not in joined}

    def usable(o):
        return all(tables[t]["columns"][c]["family"] != "int" or sparse_key(tables[t]["columns"][c], cfg)
                   for t, c in (o["a"], o["b"]))

    def evidence(o):
        (ta, ca), (tb, cb) = o["a"], o["b"]
        names = max(name_similarity(ca, tb, cb, cfg)[0], name_similarity(cb, ta, ca, cfg)[0]) if use_names else 0
        return 2 * names + o["shared"] / min(o["a_distinct"], o["b_distinct"]) + math.log10(o["shared"]) / 4

    ranked = sorted(filter(usable, overlaps), key=lambda o: (-evidence(o), o["a"], o["b"]))
    chosen = {}
    for t in sorted(isolated):
        partners = set()
        for o in ranked:
            if t in (o["a"][0], o["b"][0]) and len(partners) < cfg["repair_isolated"]:
                other = o["b"][0] if o["a"][0] == t else o["a"][0]
                if other not in partners:
                    partners.add(other)
                    chosen[(tuple(o["a"]), tuple(o["b"]))] = o
    records = []
    for o in chosen.values():
        # the column with fewer values is the source: it is the one more of whose values can be found
        s, t = ("a", "b") if o["a_distinct"] <= o["b_distinct"] else ("b", "a")
        p = tables[o[s][0]]["columns"][o[s][1]]
        pair = {"source": o[s], "target": o[t], "containment": o["shared"] / o[f"{s}_distinct"],
                "coverage": o["shared"] / o[f"{t}_distinct"], "source_unique": p["distinct"] == p["rows"] - p["nulls"]}
        records.append({**make_record(pair, profile, keys, use_names), "partial_overlap": True, "decision": "band",
                        "parts": {"overlap": round(pair["containment"], 4)}, "penalties": {}, "fan_out": 1.0,
                        "score": cfg["accept"] + cfg["delta"] / 2})
    return records


def score_database(profile, keys, cfg=DEFAULTS, overlaps=()):
    use_names = keys["use_names"]
    records, passed = [], []
    for pair in profile.get("containment", []):
        reason = gate(pair, profile, keys, cfg, use_names)
        rec = make_record(pair, profile, keys, use_names)
        if reason:
            rec.update(decision="rejected_gate", reason=reason)
        else:
            passed.append(rec)
        records.append(rec)
    w, adaptive, k_zero = score_candidates(passed, profile, cfg, use_names)
    cap_and_fan_out(passed, cfg)
    cap_low_distribution(passed, cfg)
    for r in passed:
        if "score" in r:
            r["decision"] = decide(r["score"], cfg)
    for r in records:
        r.setdefault("decision", "rejected_gate")
    if cfg["repair_isolated"]:
        records += repair_records(assemble(records, {"accepted", "band"}, cfg), profile, keys, overlaps, cfg, use_names)
    records.sort(key=lambda r: (ORDER[r["decision"]], -r.get("score", 0), r["source"]["table"], r["source"]["column"],
                                r["target"]["table"], r["target"]["column"]))
    counts = defaultdict(int)
    for r in records:
        counts[r["decision"]] += 1
    return {
        "db_id": profile["db_id"],
        "use_names": use_names,
        "keys_run": cfg["keys_run"],
        "weights": w,
        "adaptive": adaptive,
        "k_zero_share": round(k_zero, 4),
        "config": {k: v for k, v in cfg.items() if k != "weights"},
        "counts": dict(counts),
        "candidates": records,
        "graphs": {
            "direct": assemble(records, {"accepted"}, cfg),
            "stats": assemble(records, {"accepted", "band"}, cfg),
        },
    }


# ------------------------------------------------------------------ command line
def optional_float(text):
    """argparse type: a number, or none/off/negative for 'switched off' (None)."""
    if str(text).lower() in ("none", "off"):
        return None
    value = float(text)
    return None if value < 0 else value


def add_args(ap):
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--databases", nargs="*", help="only these db_ids")
    ap.add_argument("--run", default="default", help="output sub-folder, name it after what you changed")
    ap.add_argument("--keys-run", default=DEFAULTS["keys_run"],
                    help="Stage 2a run to read the keys from; its use_names setting is inherited")
    ap.add_argument("--accept", type=float, default=DEFAULTS["accept"], help="cutoff on the final score")
    ap.add_argument("--delta", type=float, default=DEFAULTS["delta"], help="width of the LLM band above the cutoff")
    ap.add_argument("--containment-min", type=float, default=DEFAULTS["containment_min"], help="G6, tau")
    ap.add_argument("--max-targets", type=int, default=DEFAULTS["max_targets"], help="G5")
    ap.add_argument("--self-refs", action="store_true", help="also consider source and target in one table")
    ap.add_argument("--pk-source-max-density", type=float, default=DEFAULTS["pk_source_max_density"],
                    help="G8 spares integer primary keys at most this dense; a negative value keeps G8 absolute")
    ap.add_argument("--dist-no-accept-below", type=optional_float, default=DEFAULTS["dist_no_accept_below"],
                    help="cap the score of a pair whose distribution term is below this at accept + delta - 1 "
                         "(never auto-accepted); 'none' (or negative) disables the cap")
    ap.add_argument("--min-range-span", type=float, default=DEFAULTS["min_range_span"],
                    help="G9 hard gate, off by default: integer source range / target range at least this (e.g. 0.1)")
    ap.add_argument("--g4-ratio", type=float, help="G4 on: source rows < ratio x target rows ...")
    ap.add_argument("--g4-multiplier", type=float, help="... multiplies the score by this (needs --g4-ratio)")
    ap.add_argument("--repair-isolated", type=int, default=DEFAULTS["repair_isolated"],
                    help="band records from out/overlaps for tables left without a join, up to this many partners each")
    ap.add_argument("--reprofile", action="store_true", help="recompute Stage 1 profiles")


def config_from(args):
    return {**DEFAULTS, "accept": args.accept, "delta": args.delta, "containment_min": args.containment_min,
            "max_targets": args.max_targets, "self_refs": args.self_refs, "keys_run": args.keys_run,
            "pk_source_max_density": None if args.pk_source_max_density is None or args.pk_source_max_density < 0 else args.pk_source_max_density,
            "min_range_span": None if args.min_range_span is None or args.min_range_span < 0 else args.min_range_span,
            "dist_no_accept_below": args.dist_no_accept_below, "repair_isolated": args.repair_isolated,
            "g4_ratio": args.g4_ratio, "g4_multiplier": args.g4_multiplier}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_args(ap)
    args = ap.parse_args()
    if (args.g4_ratio is None) != (args.g4_multiplier is None):
        ap.error("--g4-ratio and --g4-multiplier go together")
    cfg = config_from(args)
    keys_dir = ROOT / "out" / "keys" / args.dataset / args.keys_run
    out_dir = ROOT / "out" / "joins" / args.dataset / args.run
    out_dir.mkdir(parents=True, exist_ok=True)
    for db in work_list(args):
        keys_path = keys_dir / f"{db['db_id']}.json"
        if not keys_path.exists():
            sys.exit(f"{keys_path.relative_to(ROOT)} is missing; run src/primary_keys.py --dataset {args.dataset} "
                     f"--run {args.keys_run} first")
        overlaps = []
        if cfg["repair_isolated"]:
            overlaps = json.loads((ROOT / "out" / "overlaps" / args.dataset / f"{db['db_id']}.json").read_text())
        joins = score_database(load_profile(args.dataset, db, args.reprofile), json.loads(keys_path.read_text()), cfg,
                               overlaps)
        c = joins["counts"]
        print(f"  joins {db['db_id']}: {c.get('accepted', 0)} accepted, {c.get('band', 0)} in the LLM band, "
              f"{c.get('rejected_score', 0)} below {cfg['accept']:g}, {c.get('rejected_gate', 0)} gated out; "
              f"{len(joins['graphs']['stats'])} edges (stats){' [adaptive weights]' if joins['adaptive'] else ''}")
        (out_dir / f"{db['db_id']}.json").write_text(json.dumps(joins, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
