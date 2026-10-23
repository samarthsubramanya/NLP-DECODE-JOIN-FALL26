"""Stage 1: column profiles from full-column SQL. No LLM.

Each column gets null/placeholder shares, exact distinct count and uniqueness,
top values, a value-inferred type family (declared SQLite-era types are loose),
type-specific stats, a value pattern, and fixed-seed value samples. Each table
keeps a few sample rows so later prompts can show a column next to its neighbours.
"""

from poc.catalog import q, names_carry_meaning

INT_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "INT")
FLOAT_TYPES = ("DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC")
DATE_TYPES = ("DATE", "TIMESTAMP", "TIME")
BOOL_VALUES = {"0", "1", "true", "false", "t", "f", "y", "n", "yes", "no"}
UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
CODE_RE = r"^[A-Z0-9_\-\.]+$"
CAST_SHARE = 0.95


def declared_family(dtype):
    t = dtype.upper()
    if t.startswith(INT_TYPES):
        return "int"
    if t.startswith(FLOAT_TYPES):
        return "float"
    if t.startswith(DATE_TYPES):
        return "date"
    if t.startswith("BOOL"):
        return "boolean"
    return "text"


def profile_column(cat, table, col, cfg):
    p = cfg["profile"]
    c, t = q(col["name"]), q(table)
    v = f"CAST({c} AS VARCHAR)"
    real = f"{c} IS NOT NULL AND NOT list_contains(?, {v})"

    n, nonnull, distinct, placeholders = cat.scalar(
        f"SELECT count(*), count({c}), count(DISTINCT {c}), "
        f"count(*) FILTER (WHERE {c} IS NOT NULL AND list_contains(?, {v})) FROM {t}",
        [p["placeholders"]],
    )
    out = {
        "name": col["name"],
        "declared_type": col["type"],
        "position": col["position"],
        "rows": n,
        "nulls": n - nonnull,
        "null_rate": (n - nonnull) / n if n else 1.0,
        "placeholder_rate": placeholders / n if n else 0.0,
        "distinct": distinct,
        "uniqueness": distinct / nonnull if nonnull else 0.0,
    }

    out["top_values"] = [
        [val, cnt] for val, cnt in cat.rows(
            f"SELECT {v}, count(*) FROM {t} WHERE {c} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {int(p['top_k'])}"
        )
    ]
    out["sample_values"] = [
        r[0] for r in cat.rows(
            f"SELECT DISTINCT {v} AS s FROM {t} WHERE {c} IS NOT NULL ORDER BY hash(s || ?) LIMIT {int(p['sample_values'])}",
            [str(p["seed"])],
        )
    ]

    family = declared_family(col["type"])
    if family == "text" and nonnull:
        as_int, as_float, as_date = cat.scalar(
            f"SELECT avg((TRY_CAST({c} AS BIGINT) IS NOT NULL)::INT), avg((TRY_CAST({c} AS DOUBLE) IS NOT NULL)::INT), "
            f"avg((TRY_CAST({c} AS TIMESTAMP) IS NOT NULL)::INT) FROM {t} WHERE {real}",
            [p["placeholders"]],
        )
        if (as_int or 0) >= CAST_SHARE:
            family = "int"
        elif (as_float or 0) >= CAST_SHARE:
            family = "float"
        elif (as_date or 0) >= CAST_SHARE:
            family = "date"

    if family in ("int", "float") and nonnull:
        lo, hi, mean, quart, integral = cat.scalar(
            f"SELECT min(x), max(x), avg(x), quantile_cont(x, [0.25, 0.5, 0.75]), avg((x = floor(x))::INT) "
            f"FROM (SELECT TRY_CAST({c} AS DOUBLE) AS x FROM {t} WHERE {real}) WHERE x IS NOT NULL",
            [p["placeholders"]],
        )
        if family == "float" and integral == 1:
            family = "int"
        out["numeric"] = {"min": lo, "max": hi, "mean": mean, "p25": quart[0], "p50": quart[1], "p75": quart[2]} if quart else None
    elif family == "date" and nonnull:
        lo, hi = cat.scalar(f"SELECT min(TRY_CAST({c} AS TIMESTAMP)), max(TRY_CAST({c} AS TIMESTAMP)) FROM {t} WHERE {real}", [p["placeholders"]])
        out["date_range"] = [str(lo), str(hi)]

    if nonnull:
        avg_len, max_len, spaced, uuid, code = cat.scalar(
            f"SELECT avg(length({v})), max(length({v})), avg(contains({v}, ' ')::INT), "
            f"avg(regexp_matches({v}, ?)::INT), avg(regexp_matches({v}, ?)::INT) FROM {t} WHERE {real}",
            [UUID_RE, CODE_RE, p["placeholders"]],
        )
        out["text"] = {"avg_len": avg_len, "max_len": max_len, "space_share": spaced, "uuid_share": uuid, "code_share": code}

    out["family"] = family
    out["role"] = role_of(out, cfg)
    out["pattern"] = pattern_of(out)
    return out


def role_of(prof, cfg):
    fam, text = prof["family"], prof.get("text") or {}
    values = {str(val).lower() for val, _ in prof["top_values"]}
    if prof["distinct"] <= 2 and values <= BOOL_VALUES or fam == "boolean":
        return "boolean"
    if fam == "date":
        return "date"
    if fam == "float":
        return "float"
    if fam == "int":
        return "int"
    if (text.get("avg_len") or 0) >= cfg["profile"]["free_text_min_len"] and (text.get("space_share") or 0) > 0.5:
        return "free_text"
    if (text.get("avg_len") or 99) <= cfg["profile"]["code_max_len"] and (text.get("code_share") or 0) > 0.9:
        return "code"
    return "text"


def pattern_of(prof):
    num, text = prof.get("numeric") or {}, prof.get("text") or {}
    if prof["family"] == "int" and num and num["max"] is not None:
        if num["max"] - num["min"] + 1 == prof["distinct"]:
            return "gapless_int"
        return "int"
    if (text.get("uuid_share") or 0) > 0.95:
        return "uuid"
    if prof["role"] == "code":
        return "code"
    return "other"


def run(cat, cfg, log=print):
    tables = {}
    for table in cat.tables():
        cols = cat.columns(table)
        tables[table] = {
            "rows": cat.row_count(table),
            "columns": {c["name"]: profile_column(cat, table, c, cfg) for c in cols},
            "sample_rows": cat.sample_rows(table, cfg["profile"]["sample_rows"], cfg["profile"]["seed"]),
        }
    all_columns = [c for t in tables.values() for c in t["columns"]]
    result = {"db_id": cat.db_id, "names_carry_meaning": names_carry_meaning(all_columns, cfg), "tables": tables}
    log(f"  profile: {len(tables)} tables, {len(all_columns)} columns, names {'used' if result['names_carry_meaning'] else 'opaque'}")
    return result
