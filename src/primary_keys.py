"""
Stage 2a: single-column primary keys from Stage 1 profiles. No LLM, no declared keys.

Score (adapted from DBAutoDoc):
    u      = |distinct(c)| / |rows(T)|
    reject   any null, most values empty or zero, or a name on the semantic blacklist
             (date/time, quantity, financial, descriptive text)
    s_PK   = 50·f(u) + 20·n + 15·d + 15·p
      f(u) = u for u >= 0.95, linear from (u_floor, 0) to (0.95, 0.95) below that
      n    = 1 if the name matches the PK pattern (.*[Ii][Dd]$)
      d    = INT/BIGINT/UUID 1.0, VARCHAR 0.6, TEXT/BLOB 0.2, other 0.3
      p    = sequential int / UUID 15, natural key code 10, composite-derived 5 (all /15)
    then   × 0.7 if nulls, × 0.5 if unique but the name is atypical, × (1 - 0.6·l_FK)
           + 20 surrogate boost when the name is `id` or `{table}_id`, u >= 0.95, d >= 0.9
           × phi(position): 1.0, 0.85, 0.70, then 0.55
    accept s >= 70; the best accepted column is the table's primary key.

Optional rules (--rules; default R1 R4 R5, chosen on Spider dev only; `--rules` with no
values = the score above). Each has a values-only part, so it
also works when names carry no meaning; names can only add to what the values support.
  R1 referenced    another column (any table) has >= 95% of its values inside this one
                   (Stage 1 containment). Small integers overlap by chance, so an int target
                   needs >= 10 distinct values, a text target >= 2. Effect: +20 and no
                   atypical-name penalty.
  R2 only candidate  if nothing reaches 70 and exactly one column of a table with >= 20 rows
                   passes the hard rejections, is unique and is not boolean/date/float/free
                   text, it is the key (evidence "only_candidate").
  R3 value-checked penalties  the blacklist only rejects when the values agree (date/float
                   family, or boolean/date/float/free-text role); the FK penalty is dropped
                   when the column is referenced. (Also dropping it for a table's only unique
                   column was tried and removed: values cannot tell a 1:1 extension table
                   from a junction table, and it added a false key on Spider pets_1.Has_Pet.)
  R4 key-like names  n = 1 also for names ending in code/key/number/nbr, or a last word no/num.
  R5 empty tables  a table without rows gets the one column named `id` or `{table}<key suffix>`,
                   marked evidence "name_only" (graded separately).

Choices the paper leaves open (all in DEFAULTS, change them there or on the CLI):
  - f(u) has no lower anchor in the paper: u_floor = 0.5 maps to 0.
  - p is given in points (15/10/5) and weighted by 15, so it is divided by 15.
  - "sequential" = integer values filling >= seq_min_density of their [min, max] range.
  - natural key code = short code-like strings: profile role "code", or a VARCHAR whose
    values are all upper/digit codes without spaces up to code_max_len (e.g. BIRD CDSCode).
  - DuckDB stores SQLite TEXT as VARCHAR, so the TEXT score (0.2) only fires for BLOB.
  - The null penalty is unreachable while reject_nulls is on (any null is a hard reject).
  - The blacklist is matched on name tokens (snake_case/camelCase parts) and skipped for
    names that match the PK pattern, so payment_id or date_id are not rejected.
  - l_FK comes from names: 1.0 when the name minus its id suffix names another table
    of the database (account.district_id -> district), else 0.
  - The boost is added after the penalties and before phi; {table}_id also accepts
    {table}id and the singular table name (customers.customer_id).
  - --no-names simulates the obfuscated setting: n's weight is spread over f, d and p,
    and the blacklist, atypical-name penalty, l_FK and boost are off.

Usage:
    python src/primary_keys.py --dataset bird
    python src/primary_keys.py --dataset spider --rules R1 R3 --run r1_r3
    python src/primary_keys.py --dataset spider --rules --run baseline      # no rules

Input:  out/profiles/<dataset>/<db_id>.json   (src/profile_columns.py; made here if missing)
Output: out/keys/<dataset>/<run>/<db_id>.json
"""
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from duckdb_driver import open_db
from profile_columns import DEFAULTS as PROFILE_DEFAULTS, INT_TYPES, NOT_KEY_ROLES, profile_database

ROOT = Path(__file__).resolve().parent.parent

RULES = ("R1", "R2", "R3", "R4", "R5")

DEFAULTS = {
    "accept": 70,
    "weights": {"u": 50, "name": 20, "dtype": 15, "pattern": 15},
    "u_floor": 0.5,
    "u_full": 0.95,
    "pk_name_pattern": r"[Ii][Dd]$",
    "reject_nulls": True,
    "empty_or_zero_max": 0.5,         # "most values are empty or zero"
    "seq_min_density": 0.9,
    "code_max_len": 20,
    "null_penalty": 0.7,
    "atypical_name_penalty": 0.5,
    "fk_penalty": 0.6,
    "surrogate_boost": 20,
    "position_multiplier": [1.0, 0.85, 0.70, 0.55],
    "use_names": True,
    # frozen 2026-10-23 from the Spider dev ablation (docs/column_profiling.md §6); `--rules` alone = none
    "rules": ["R1", "R4", "R5"],
    # R1
    "ref_min_containment": 0.95,
    "ref_min_distinct": {"int": 10, "text": 2},
    "ref_bonus": 20,
    # R2
    "only_candidate_min_rows": 20,
    # R4
    "key_name_pattern": r"(?i)(id|code|key|number|nbr)$",
    "key_name_last_words": ["no", "num"],
}

BLACKLIST = {
    "date/time": {"date", "time", "datetime", "timestamp", "year", "month", "day", "hour", "minute",
                  "created", "updated", "modified", "dob", "birthday", "birthdate"},
    "quantity": {"qty", "quantity", "count", "cnt", "total", "sum", "size", "weight", "height",
                 "length", "width", "volume", "age", "percent", "pct", "ratio", "rate"},
    "financial": {"price", "cost", "amount", "amt", "balance", "salary", "revenue", "fee", "payment",
                  "tax", "budget", "income", "profit", "wage", "debt", "loan"},
    "descriptive text": {"description", "desc", "comment", "comments", "note", "notes", "remark",
                         "remarks", "text", "summary", "body", "title", "address", "bio", "content", "message"},
}

DTYPE_SCORE = [  # first prefix match wins
    (INT_TYPES + ("UUID", "UNIQUEIDENTIFIER"), 1.0),
    (("VARCHAR", "CHAR", "STRING", "NVARCHAR"), 0.6),
    (("TEXT", "BLOB", "BYTEA", "CLOB"), 0.2),
]


def tokens(name):
    """'districtId' / 'DISTRICT_ID' / 'Free Meal Count' -> lower-case word parts."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [t.lower() for t in re.split(r"[^A-Za-z0-9]+", spaced) if t]


def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def singular(word):
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("ses") or word.endswith("xes"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def table_forms(table):
    n = norm(table)
    return {n, singular(n)}


def key_like_name(name, cfg):
    """n = 1: the PK pattern (`...id`), or with R4 also code/key/number/nbr endings and no/num."""
    if re.search(cfg["pk_name_pattern"], name):
        return True
    if "R4" in cfg["rules"]:
        words = tokens(name)
        return bool(re.search(cfg["key_name_pattern"], name)) or bool(words and words[-1] in cfg["key_name_last_words"])
    return False


def id_stem(name, cfg):
    """'district_id' -> 'district', 'MONETARYAMOUNTID' -> 'monetaryamount', 'id' -> ''."""
    if "R4" in cfg["rules"]:
        words = tokens(name)
        if words and words[-1] in cfg["key_name_last_words"]:
            return norm("".join(words[:-1]))
        return norm(re.sub(cfg["key_name_pattern"], "", name))
    return norm(re.sub(cfg["pk_name_pattern"], "", name))


def dtype_score(declared):
    t = declared.upper()
    for prefixes, score in DTYPE_SCORE:
        if t.startswith(prefixes):
            return score
    return 0.3


def pattern_score(prof, cfg):
    num, text = prof.get("numeric") or {}, prof.get("text") or {}
    if prof["pattern"] == "uuid":
        return 15, "uuid"
    if prof["family"] == "int" and (num.get("density") or 0) >= cfg["seq_min_density"]:
        return 15, "sequential_int"
    if prof["pattern"] == "composite":
        return 5, "composite"
    is_code = prof["role"] == "code" or (
        prof["role"] != "date"
        and not prof["declared_type"].upper().startswith(INT_TYPES)
        and (text.get("code_share") or 0) > 0.9
        and (text.get("space_share") or 0) == 0
        and (text.get("max_len") or 99) <= cfg["code_max_len"]
    )
    if is_code:
        return 10, "natural_code"
    return 0, "none"


def f_u(u, cfg):
    if u >= cfg["u_full"]:
        return u
    lo, hi = cfg["u_floor"], cfg["u_full"]
    return max(0.0, hi * (u - lo) / (hi - lo))


def referenced(profile, cfg):
    """{(table, column): [source "table.column", ...]} from the Stage 1 containment pairs (R1)."""
    out = defaultdict(list)
    for pair in profile.get("containment", []):
        tt, tc = pair["target"]
        family = profile["tables"][tt]["columns"][tc]["family"]
        if (pair["containment"] >= cfg["ref_min_containment"]
                and pair["target_distinct"] >= cfg["ref_min_distinct"].get(family, 2)):
            out[(tt, tc)].append(".".join(pair["source"]))
    return out


def hard_reject(prof, cfg, use_names):
    if prof["rows"] == 0:
        return "empty table"
    if cfg["reject_nulls"] and prof["nulls"]:
        return "has nulls"
    if prof["placeholder_rate"] + prof["zero_rate"] > cfg["empty_or_zero_max"]:
        return "mostly empty or zero"
    if use_names and not key_like_name(prof["name"], cfg):
        words = set(tokens(prof["name"]))
        values_agree = prof["family"] in ("date", "float") or prof["role"] in NOT_KEY_ROLES
        for group, banned in BLACKLIST.items():
            if words & banned and ("R3" not in cfg["rules"] or values_agree):
                return f"blacklist: {group} ({', '.join(sorted(words & banned))})"
    return None


def score_column(prof, table, other_tables, cfg, use_names, referenced_by=()):
    """{"score", "parts", "multipliers", ...} for a column that passed hard_reject."""
    rules = cfg["rules"]
    w = dict(cfg["weights"])
    if not use_names:
        spill = w.pop("name")
        total = sum(w.values())
        w = {k: v + spill * v / total for k, v in w.items()}

    u = prof["distinct"] / prof["rows"]
    p_points, p_kind = pattern_score(prof, cfg)
    parts = {"u": u, "f_u": f_u(u, cfg), "dtype": dtype_score(prof["declared_type"]),
             "pattern": p_points / 15, "pattern_kind": p_kind}
    base = w["u"] * parts["f_u"] + w["dtype"] * parts["dtype"] + w["pattern"] * parts["pattern"]
    if use_names:
        parts["name"] = 1.0 if key_like_name(prof["name"], cfg) else 0.0
        base += w["name"] * parts["name"]
    is_referenced = "R1" in rules and bool(referenced_by)
    if referenced_by:
        parts["referenced_by"] = list(referenced_by)

    mult = {}
    if prof["nulls"]:
        mult["nulls"] = cfg["null_penalty"]
    boost = 0
    if use_names:
        stem = id_stem(prof["name"], cfg)
        own = table_forms(table)
        if u == 1.0 and not parts["name"] and not is_referenced:
            mult["atypical_name"] = cfg["atypical_name_penalty"]
        l_fk = 1.0 if stem and stem not in own and any(stem in table_forms(t) for t in other_tables) else 0.0
        if "R3" in rules and referenced_by:
            l_fk = 0.0
        if l_fk:
            mult["fk_likelihood"] = 1 - cfg["fk_penalty"] * l_fk
        surrogate = norm(prof["name"]) == "id" or (parts["name"] and stem in own)
        if surrogate and u >= cfg["u_full"] and parts["dtype"] >= 0.9:
            boost = cfg["surrogate_boost"]
    if is_referenced:
        boost += cfg["ref_bonus"]

    phis = cfg["position_multiplier"]
    phi = phis[min(prof["position"], len(phis) - 1)]
    s = base
    for m in mult.values():
        s *= m
    s = (s + boost) * phi
    return {"score": round(s, 3), "base": round(base, 3), "parts": parts,
            "multipliers": mult, "boost": boost, "phi": phi}


def score_database(profile, cfg=DEFAULTS):
    use_names, rules = cfg["use_names"], cfg["rules"]
    refs = referenced(profile, cfg)
    tables = {}
    names = list(profile["tables"])
    for table, t in profile["tables"].items():
        others = [o for o in names if o != table]
        cols = t["columns"]
        rejected = {c: hard_reject(p, cfg, use_names) for c, p in cols.items()}
        unique = [c for c, p in cols.items() if not rejected[c]
                  and p["distinct"] == p["rows"] and p["role"] not in NOT_KEY_ROLES]
        scored = {
            c: {"rejected": rejected[c]} if rejected[c] else
            score_column(p, table, others, cfg, use_names, refs.get((table, c), []))
            for c, p in cols.items()
        }
        accepted = sorted(
            ((c, s["score"]) for c, s in scored.items() if s.get("score", -1) >= cfg["accept"]),
            key=lambda x: (-x[1], cols[x[0]]["position"]),
        )
        pk, evidence = (accepted[0][0], "score") if accepted else (None, None)
        if pk is None and "R2" in rules and t["rows"] >= cfg["only_candidate_min_rows"] and len(unique) == 1:
            pk, evidence = unique[0], "only_candidate"
        if pk is None and "R5" in rules and use_names and t["rows"] == 0:
            named = [c for c in cols if norm(c) == "id"
                     or (key_like_name(c, cfg) and id_stem(c, cfg) in table_forms(table))]
            if len(named) == 1:
                pk, evidence = named[0], "name_only"
        tables[table] = {
            "primary_key": pk,
            "evidence": evidence,
            "candidate_keys": [{"column": c, "score": s} for c, s in accepted],
            "columns": scored,
        }
    return {"db_id": profile["db_id"], "use_names": use_names, "rules": list(rules),
            "accept": cfg["accept"], "tables": tables}


def load_profile(dataset, db, force=False):
    """Profile from out/profiles/<dataset>/, made (and saved) first if missing."""
    path = ROOT / "out" / "profiles" / dataset / f"{db['db_id']}.json"
    if path.exists() and not force:
        return json.loads(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    prof = profile_database(open_db(db["duckdb_file"]), db["duckdb_schema"], db["tables"], PROFILE_DEFAULTS, db["db_id"])
    prof.update(dataset=db["dataset"], duckdb_file=db["duckdb_file"])
    path.write_text(json.dumps(prof, indent=1, sort_keys=True, default=str))
    return json.loads(path.read_text())   # same types as a cached load (dates become strings)


def add_args(ap):
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--databases", nargs="*", help="only these db_ids")
    ap.add_argument("--run", default="default", help="output sub-folder, name it after what you changed")
    ap.add_argument("--rules", nargs="*", default=DEFAULTS["rules"], choices=RULES,
                    help="optional rules, see the module docstring (default: the frozen set; give no values for none)")
    ap.add_argument("--no-names", action="store_true", help="ignore column names (obfuscated-setting simulation)")
    ap.add_argument("--accept", type=float, default=DEFAULTS["accept"])
    ap.add_argument("--u-floor", type=float, default=DEFAULTS["u_floor"])
    ap.add_argument("--seq-min-density", type=float, default=DEFAULTS["seq_min_density"])
    ap.add_argument("--allow-nulls", action="store_true", help="nulls get the x0.7 penalty instead of a hard reject")
    ap.add_argument("--reprofile", action="store_true", help="recompute Stage 1 profiles")


def config_from(args):
    return {**DEFAULTS, "use_names": not args.no_names, "accept": args.accept, "u_floor": args.u_floor,
            "seq_min_density": args.seq_min_density, "reject_nulls": not args.allow_nulls,
            "rules": sorted(set(args.rules))}


def work_list(args):
    dbs = json.loads((ROOT / "out" / "filtered" / args.dataset / "databases.json").read_text())
    return [d for d in dbs if not args.databases or d["db_id"] in args.databases]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_args(ap)
    args = ap.parse_args()
    cfg = config_from(args)
    out_dir = ROOT / "out" / "keys" / args.dataset / args.run
    out_dir.mkdir(parents=True, exist_ok=True)
    for db in work_list(args):
        keys = score_database(load_profile(args.dataset, db, args.reprofile), cfg)
        found = sum(t["primary_key"] is not None for t in keys["tables"].values())
        print(f"  keys {db['db_id']}: primary key for {found}/{len(keys['tables'])} tables")
        (out_dir / f"{db['db_id']}.json").write_text(json.dumps(keys, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
