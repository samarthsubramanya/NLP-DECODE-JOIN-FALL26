"""Stage 2b ablation arms (hp): each arm changes ONE component of FULL and keeps everything else.

An arm is a function (profile, keys, cfg, use_names) -> (profile, keys, cfg). Profiles and configurations are
copied, never edited in place, and the scorer is the unchanged foreign_keys.score_database. Arms that remove a score
term spread its points over the remaining terms in proportion, so the total stays 100 (as the 2026-11-04
leave-one-out did). Arms that do not apply to a setting (the name term when names are hidden) return None.

    full                  FULL, the frozen defaults
    no_qmd                QuantileMD removed: no distribution evidence on any pair (its points go to overlap,
                          no low-distribution cap). Same as evals/ablation/variants.py no-qmd
    no_dist_cap           distribution term kept, the cap at 69 off
    no_name               name-similarity term removed (named settings only)
    no_cardinality        rho term removed
    no_target_key         "target is the chosen PK" term removed
    no_nulls              null-share term removed
    no_orphan_penalty     x0.7 for orphan rate above 20% off
    no_fan_out            psi(n) = 1 for every number of targets
    no_g5                 no limit on targets per source
    no_g8_exception       G8 absolute: no primary key is ever a source, sparse or not
    no_adaptive_weights   the k = 0 shift of target-key points to overlap never fires
    tau_0.5               G6 at 0.5 (the lowest containment Stage 1 stores)
    adapt_<rule>          sparsity-dependent thresholds (adaptive.py): tau, orphan, null, band, all
"""
import foreign_keys as fk
from adaptive import RULES, adapt, signals


def base_weights(cfg, use_names):
    return dict(cfg.get("weights") or (fk.W_NAMED if use_names else fk.W_NO_NAMES))


def drop_term(term):
    def arm(profile, keys, cfg, use_names):
        w = base_weights(cfg, use_names)
        if not w.get(term):
            return None
        gone, rest = w.pop(term), sum(v for v in w.values())
        w = {k: v + gone * v / rest for k, v in w.items()}
        w[term] = 0
        return profile, keys, {**cfg, "weights": w}
    return arm


def set_cfg(**changes):
    def arm(profile, keys, cfg, use_names):
        return profile, keys, {**cfg, **changes}
    return arm


def no_qmd(profile, keys, cfg, use_names):
    p = dict(profile)
    p["containment"] = [{**x, "dist": None, "dist_buckets": None} for x in profile.get("containment", [])]
    return p, keys, cfg


def adaptive(rule):
    def arm(profile, keys, cfg, use_names):
        return profile, keys, adapt(cfg, signals(profile, keys, cfg), [rule])
    return arm


ARMS = {
    "full": lambda p, k, c, u: (p, k, c),
    "no_qmd": no_qmd,
    "no_dist_cap": set_cfg(dist_no_accept_below=None),
    "no_name": drop_term("name"),
    "no_cardinality": drop_term("cardinality"),
    "no_target_key": drop_term("target_key"),
    "no_nulls": drop_term("nulls"),
    "no_orphan_penalty": set_cfg(orphan_penalty=1.0),
    "no_fan_out": set_cfg(fan_out=[1.0, 1.0, 1.0, 1.0]),
    "no_g5": set_cfg(max_targets=10_000),
    "no_g8_exception": set_cfg(pk_source_max_density=None),
    "no_adaptive_weights": set_cfg(adaptive_k_zero_share=1.01),
    "tau_0.5": set_cfg(containment_min=0.5),
    **{f"adapt_{r}": adaptive(r) for r in (*RULES, "all")},
}
CORE = ("full", "no_qmd", "no_dist_cap", "adapt_all")


def run_arm(name, profile, keys, cfg, use_names):
    """foreign_keys.score_database under one arm, or None when the arm does not apply."""
    got = ARMS[name](profile, keys, cfg, use_names)
    if got is None:
        return None
    p, k, c = got
    out = fk.score_database(p, k, c)
    out["arm"] = name
    return out
