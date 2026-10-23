"""Stage 2a: primary keys from profiles. No LLM.

A column can only be a candidate if it has no nulls and is unique. Its score mixes
uniqueness, value pattern, position in the table and an id-style name; when names
are opaque the name weight is spread over the other terms. Dates, floats, booleans
and free text are rejected. Composite keys are out of scope.
"""

import re

PATTERN_SCORE = {"gapless_int": 1.0, "uuid": 1.0, "int": 0.6, "code": 0.5, "other": 0.2}


def weights(cfg, use_names):
    w = dict(cfg["keys"]["weights"])
    if not use_names:
        spill = w.pop("name")
        total = sum(w.values())
        w = {k: v + spill * v / total for k, v in w.items()}
    return w


def score_column(prof, cfg, use_names):
    if prof["nulls"] or prof["rows"] == 0 or prof["uniqueness"] < 1.0 or prof["placeholder_rate"] > 0:
        return None, "not complete and unique"
    if prof["role"] in cfg["keys"]["reject_roles"]:
        return None, f"role {prof['role']}"
    parts = {
        "uniqueness": prof["uniqueness"],
        "pattern": PATTERN_SCORE.get(prof["pattern"], 0.2),
        "position": 1.0 / prof["position"],
    }
    if use_names:
        parts["name"] = 1.0 if re.search(cfg["keys"]["id_name_pattern"], prof["name"], re.I) else 0.0
    w = weights(cfg, use_names)
    return {"score": sum(w[k] * v for k, v in parts.items()), "parts": parts}, None


def run(profiles, cfg, log=print):
    use_names = profiles["names_carry_meaning"]
    tables = {}
    for table, t in profiles["tables"].items():
        candidates, rejected = [], {}
        for name, prof in t["columns"].items():
            scored, why = score_column(prof, cfg, use_names)
            if scored and scored["score"] >= cfg["keys"]["min_score"]:
                candidates.append({"column": name, **scored})
            else:
                rejected[name] = why or f"score {scored['score']:.2f} < {cfg['keys']['min_score']}"
        candidates.sort(key=lambda c: -c["score"])
        tables[table] = {
            "primary_key": candidates[0]["column"] if candidates else None,
            "candidate_keys": candidates,
            "rejected": rejected,
        }
    n_pk = sum(t["primary_key"] is not None for t in tables.values())
    log(f"  keys: primary key found for {n_pk}/{len(tables)} tables")
    return {"db_id": profiles["db_id"], "use_names": use_names, "tables": tables}
