"""
Stage 1: column profiles from full-column SQL. No LLM. Port of poc/profile.py to the
shared DuckDB files (one schema per database) and the filtered work list.

Each column gets null/placeholder/zero shares, exact distinct count and uniqueness,
top values, a value-inferred type family (declared SQLite-era types are loose),
type-specific stats, value-pattern signals, and fixed-seed value samples. Each table
keeps a few sample rows so later prompts can show a column next to its neighbours.

Only table names, column names, types and rows are read (information_schema.columns
and the tables themselves). Declared constraints, schema_mapping.csv and the
*_metadata schemas are never touched: those are evaluation labels.

Usage:
    python src/profile_columns.py --dataset bird
    python src/profile_columns.py --dataset spider --databases concert_singer pets_1

    python src/profile_columns.py --dataset beaver --overlaps    # partial value overlaps only

Input:  out/filtered/<dataset>/databases.json   (written by filter_for_multitable_datasets.py)
Output: out/profiles/<dataset>/<db_id>.json
        out/overlaps/<dataset>/<db_id>.json   (--overlaps; read by foreign_keys.py --repair-isolated)
"""
import argparse
import json
import time
from pathlib import Path

from duckdb_driver import open_db

ROOT = Path(__file__).resolve().parent.parent

INT_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "INT")
FLOAT_TYPES = ("DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC")
DATE_TYPES = ("DATE", "TIMESTAMP", "TIME")
BOOL_VALUES = {"0", "1", "true", "false", "t", "f", "y", "n", "yes", "no"}
UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
CODE_RE = r"^[A-Z0-9_\-\.]+$"
# two or more alphanumeric parts joined by a separator, e.g. "AB-1023", "2019/07/A"
COMPOSITE_RE = r"^[A-Za-z0-9]+([\-_/:|.][A-Za-z0-9]+)+$"
# year ranges and month-year values that do not cast to TIMESTAMP: "2014-2015", "2014/15", "07/2019"
DATE_SHAPE_RE = r"^((18|19|20)\d{2}[\-/](\d{2}|(18|19|20)\d{2})|(0?[1-9]|1[0-2])[\-/](18|19|20)\d{2})$"
CAST_SHARE = 0.95
# containment is only computed between columns of these families, toward key-like targets
KEY_FAMILIES = ("int", "text")
NOT_KEY_ROLES = ("boolean", "date", "float", "free_text")

DEFAULTS = {
    "top_k": 10,
    "sample_values": 10,
    "sample_rows": 5,
    "seed": 42,
    "placeholders": ["", "N/A", "NA", "n/a", "null", "NULL", "None", "-", "?", "unknown", "Unknown"],
    "free_text_min_len": 30,  # avg length above this with spaces = free text
    "code_max_len": 12,       # short, mostly upper/digits = code
    "containment_store_min": 0.5,   # pairs below this are not written
    "dist_max_buckets": 10,         # quantile-EMD distribution term: at most this many target buckets ...
    "dist_min_buckets": 5,          # ... and fewer than this many means no evidence (dist = None)
    "overlap_min_distinct": 5,      # partial_overlaps: columns with fewer distinct values are skipped
    "overlap_min_shared": 10,       # ... and a pair needs this many shared values
    "overlap_min_share": 0.05,      # ... and this share of the smaller column's values
}


def q(name):
    return '"' + name.replace('"', '""') + '"'


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


def columns(con, schema, table):
    rows = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
        [schema, table],
    ).fetchall()
    # position is 0-based: 0 = first column of the table
    return [{"name": n, "type": t, "position": i} for i, (n, t) in enumerate(rows)]


def profile_column(con, schema, table, col, cfg):
    def one(sql, params=None):
        return con.execute(sql, params or []).fetchone()

    def many(sql, params=None):
        return con.execute(sql, params or []).fetchall()

    c, t = q(col["name"]), f"{q(schema)}.{q(table)}"
    v = f"CAST({c} AS VARCHAR)"
    real = f"{c} IS NOT NULL AND NOT list_contains(?, {v})"
    ph = [cfg["placeholders"]]

    n, nonnull, distinct, placeholders, zeros = one(
        f"SELECT count(*), count({c}), count(DISTINCT {c}), "
        f"count(*) FILTER (WHERE {c} IS NOT NULL AND list_contains(?, {v})), "
        f"count(*) FILTER (WHERE TRY_CAST({v} AS DOUBLE) = 0) FROM {t}",
        ph,
    )
    out = {
        "name": col["name"],
        "declared_type": col["type"],
        "position": col["position"],
        "rows": n,
        "nulls": n - nonnull,
        "null_rate": (n - nonnull) / n if n else 1.0,
        "placeholder_rate": placeholders / n if n else 0.0,
        "zero_rate": zeros / n if n else 0.0,
        "distinct": distinct,
        "uniqueness": distinct / nonnull if nonnull else 0.0,
    }

    out["top_values"] = [
        [val, cnt] for val, cnt in many(
            f"SELECT {v}, count(*) FROM {t} WHERE {c} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {int(cfg['top_k'])}"
        )
    ]
    out["sample_values"] = [
        r[0] for r in many(
            f"SELECT DISTINCT {v} AS s FROM {t} WHERE {c} IS NOT NULL ORDER BY hash(s || ?) LIMIT {int(cfg['sample_values'])}",
            [str(cfg["seed"])],
        )
    ]

    family = declared_family(col["type"])
    if family == "text" and nonnull:
        as_int, as_float, as_date = one(
            f"SELECT avg((TRY_CAST({c} AS BIGINT) IS NOT NULL)::INT), avg((TRY_CAST({c} AS DOUBLE) IS NOT NULL)::INT), "
            f"avg((TRY_CAST({c} AS TIMESTAMP) IS NOT NULL)::INT) FROM {t} WHERE {real}",
            ph,
        )
        if (as_int or 0) >= CAST_SHARE:
            family = "int"
        elif (as_float or 0) >= CAST_SHARE:
            family = "float"
        elif (as_date or 0) >= CAST_SHARE:
            family = "date"

    if family in ("int", "float") and nonnull:
        lo, hi, mean, quart, integral = one(
            f"SELECT min(x), max(x), avg(x), quantile_cont(x, [0.25, 0.5, 0.75]), avg((x = floor(x))::INT) "
            f"FROM (SELECT TRY_CAST({c} AS DOUBLE) AS x FROM {t} WHERE {real}) WHERE x IS NOT NULL",
            ph,
        )
        if family == "float" and integral == 1:
            family = "int"
        out["numeric"] = {"min": lo, "max": hi, "mean": mean, "p25": quart[0], "p50": quart[1], "p75": quart[2]} if quart else None
        if family == "int" and out["numeric"] and hi is not None:
            # share of the [min, max] range that is used: 1.0 = gapless run
            out["numeric"]["density"] = distinct / (hi - lo + 1) if hi - lo + 1 > 0 else 0.0
    elif family == "date" and nonnull:
        lo, hi = one(f"SELECT min(TRY_CAST({c} AS TIMESTAMP)), max(TRY_CAST({c} AS TIMESTAMP)) FROM {t} WHERE {real}", ph)
        out["date_range"] = [str(lo), str(hi)]

    if nonnull:
        avg_len, min_len, max_len, spaced, uuid, code, composite, date_shape = one(
            f"SELECT avg(length({v})), min(length({v})), max(length({v})), avg(contains({v}, ' ')::INT), "
            f"avg(regexp_matches({v}, ?)::INT), avg(regexp_matches({v}, ?)::INT), avg(regexp_matches({v}, ?)::INT), "
            f"avg(regexp_matches({v}, ?)::INT) FROM {t} WHERE {real}",
            [UUID_RE, CODE_RE, COMPOSITE_RE, DATE_SHAPE_RE, *ph],
        )
        out["text"] = {"avg_len": avg_len, "min_len": min_len, "max_len": max_len, "space_share": spaced,
                       "uuid_share": uuid, "code_share": code, "composite_share": composite,
                       "date_shape_share": date_shape}

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
    if (text.get("date_shape_share") or 0) > 0.9:
        return "date"
    if (text.get("avg_len") or 0) >= cfg["free_text_min_len"] and (text.get("space_share") or 0) > 0.5:
        return "free_text"
    if (text.get("avg_len") or 99) <= cfg["code_max_len"] and (text.get("code_share") or 0) > 0.9:
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
    if (text.get("composite_share") or 0) > 0.9 and prof["role"] != "date":
        return "composite"
    if prof["role"] == "code":
        return "code"
    return "other"


def sample_rows(con, schema, table, n, seed):
    cur = con.execute(f"SELECT * FROM {q(schema)}.{q(table)} USING SAMPLE reservoir({int(n)} ROWS) REPEATABLE ({int(seed)})")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, map(_plain, r))) for r in cur.fetchall()]


def _plain(v):
    return v if v is None or isinstance(v, (int, float, str, bool)) else str(v)


def key_like(prof):
    """Could be a key: rows, no nulls, every value distinct, not a boolean/date/float/free text."""
    return (prof["rows"] > 0 and prof["nulls"] == 0 and prof["distinct"] == prof["rows"]
            and prof["family"] in KEY_FAMILIES and prof["role"] not in NOT_KEY_ROLES)


def quantile_emd(counts):
    """Distribution score of a source's matching values over L equal-size quantile buckets of the target.

    counts[i] = how many of the source's matching distinct values fall in bucket i (buckets are the target's
    distinct values cut by ntile over their sort order). With p_i = counts_i / sum(counts) and cum_i the
    running sum of p, the earth mover's distance to a uniform spread is EMD = sum_{i<L} |cum_i - i/L|, at most
    (L - 1) / 2. Returns 1 - EMD / EMD_max in [0, 1]: 1 = spread evenly over the target's range, 0 = all in one
    end bucket. None when there is no evidence: fewer than 5 buckets (a target with fewer than 5 distinct values), or no values.
    """
    n, total = len(counts), sum(counts)
    if n < 5 or total <= 0:
        return None
    cum = emd = 0.0
    for i, c in enumerate(counts[:-1], 1):
        cum += c / total
        emd += abs(cum - i / n)
    return min(1.0, max(0.0, 1 - emd / ((n - 1) / 2)))


def bucket_expr(rows, n_buckets, rank="rn"):
    """SQL for ntile(n_buckets) over rows ranked 1..rows: the first rows % n_buckets buckets hold one extra row."""
    q, r = divmod(rows, n_buckets)
    return (f"CASE WHEN {rank} <= {r * (q + 1)} THEN ({rank} - 1) // {q + 1} + 1 "
            f"ELSE {r} + ({rank} - {r * (q + 1)} - 1) // {q} + 1 END")


def containment(con, schema, tables, cfg):
    """I(a -> b) = |Va ∩ Vb| / |Va| for every key-like target b and every same-family column a.

    Values are compared as BIGINT for int columns (so '007' = 7) and as trimmed text otherwise;
    NULLs and placeholders are left out. Pairs whose containment cannot reach
    containment_store_min (|Vb| / |Va| too small, or int ranges that do not overlap) are skipped.

    Every stored pair also gets "dist" and "dist_buckets": where the source's matching values sit in the
    target's sort order (quantile_emd). The target's distinct values are ranked once per target column; the
    L = min(dist_max_buckets, target_distinct) buckets are ntile(L) of that rank. L is not capped by the
    number of hits: a source with few distinct values that all land in one bucket scores about 0. No
    evidence (both fields None) only when the target has fewer than dist_min_buckets distinct values or
    the source has no hit.
    """
    cols = [(t, c, p) for t in sorted(tables) for c, p in tables[t]["columns"].items()]
    targets = [x for x in cols if key_like(x[2])]
    sources = [x for x in cols if x[2]["family"] in KEY_FAMILIES and x[2]["role"] not in NOT_KEY_ROLES
               and x[2]["distinct"] > 0]
    floor = cfg["containment_store_min"]

    def values(table, col, fam):
        c = q(col)
        e = f"TRY_CAST({c} AS BIGINT)" if fam == "int" else f"trim(CAST({c} AS VARCHAR))"
        return (f"SELECT DISTINCT {e} AS x FROM {q(schema)}.{q(table)} "
                f"WHERE {c} IS NOT NULL AND NOT list_contains(?, CAST({c} AS VARCHAR))")

    out = []
    for tb, cb, pb in targets:
        ranked_rows = None      # rows of the temp table _dist_rank, built on this target's first stored pair
        for ta, ca, pa in sources:
            fam = pa["family"]
            if (ta, ca) == (tb, cb) or fam != pb["family"] or pb["distinct"] < floor * pa["distinct"]:
                continue
            na_, nb_ = pa.get("numeric") or {}, pb.get("numeric") or {}
            if fam == "int" and na_ and nb_ and (na_["min"] > nb_["max"] or na_["max"] < nb_["min"]):
                continue
            n_a, inter = con.execute(
                f"SELECT count(*), count(*) FILTER (WHERE x IN (SELECT x FROM ({values(tb, cb, fam)}))) "
                f"FROM ({values(ta, ca, fam)}) WHERE x IS NOT NULL",
                [cfg["placeholders"], cfg["placeholders"]],
            ).fetchone()
            if not n_a or inter / n_a < floor:
                continue
            buckets = min(cfg["dist_max_buckets"], pb["distinct"])
            dist = counts = None
            if buckets >= cfg["dist_min_buckets"] and inter > 0:
                if ranked_rows is None:
                    con.execute(f"CREATE OR REPLACE TEMP TABLE _dist_rank AS SELECT x, row_number() OVER (ORDER BY x) AS rn "
                                f"FROM ({values(tb, cb, fam)}) WHERE x IS NOT NULL", [cfg["placeholders"]])
                    ranked_rows = con.execute("SELECT count(*) FROM _dist_rank").fetchone()[0]
                buckets = min(buckets, ranked_rows)       # placeholders in the target can leave fewer rows than distinct
            if buckets >= cfg["dist_min_buckets"] and inter > 0:
                hit = dict(con.execute(
                    f"SELECT {bucket_expr(ranked_rows, buckets)} AS b, count(*) FROM _dist_rank "
                    f"WHERE x IN (SELECT x FROM ({values(ta, ca, fam)}) WHERE x IS NOT NULL) GROUP BY 1",
                    [cfg["placeholders"]]).fetchall())
                counts = [hit.get(i, 0) for i in range(1, buckets + 1)]
                dist = quantile_emd(counts)
            out.append({
                "source": [ta, ca], "target": [tb, cb], "intersection": inter,
                "containment": inter / n_a,               # share of the source's values found in the target
                "coverage": inter / pb["distinct"],       # share of the target's values the source uses
                "source_distinct": n_a, "target_distinct": pb["distinct"],
                "source_unique": pa["distinct"] == pa["rows"] - pa["nulls"],
                "dist": dist,                             # quantile-EMD spread over the target's range, None = no evidence
                "dist_buckets": counts,                   # source hits per target quantile bucket
            })
    return out


def partial_overlaps(con, schema, tables, cfg=DEFAULTS):
    """Shared distinct values between same-family columns of different tables, with no key-like target needed.

    containment() only sees pairs into a unique, null-free column at 50% or more. Tables cut to a row cap
    independently (BEAVER dw: 10,000 rows) break both: a real parent key loses rows, so few values match, and
    join columns between two fact tables have no unique end. Thousands of shared key values still can't be chance.
    """
    def values(table, col, fam):
        c = q(col)
        e = f"TRY_CAST({c} AS BIGINT)" if fam == "int" else f"trim(CAST({c} AS VARCHAR))"
        rows = con.execute(f"SELECT DISTINCT {e} FROM {q(schema)}.{q(table)} "
                           f"WHERE {c} IS NOT NULL AND NOT list_contains(?, CAST({c} AS VARCHAR))", [cfg["placeholders"]])
        return {r[0] for r in rows.fetchall() if r[0] is not None}

    cols = {(t, c): values(t, c, p["family"]) for t, tp in tables.items() for c, p in tp["columns"].items()
            if p["family"] in KEY_FAMILIES and p["role"] not in ("boolean", "date", "float")
            and p["distinct"] >= cfg["overlap_min_distinct"]}
    keys, out = sorted(cols), []
    for i, a in enumerate(keys):       # ponytail: all pairs in memory, fine for ~1,000 columns; bucket by value if not
        fam = tables[a[0]]["columns"][a[1]]["family"]
        for b in keys[i + 1:]:
            if a[0] == b[0] or tables[b[0]]["columns"][b[1]]["family"] != fam:
                continue
            shared = len(cols[a] & cols[b])
            if shared >= cfg["overlap_min_shared"] \
                    and shared >= cfg["overlap_min_share"] * min(len(cols[a]), len(cols[b])):
                out.append({"a": list(a), "b": list(b), "a_distinct": len(cols[a]), "b_distinct": len(cols[b]),
                            "shared": shared})
    return out


def profile_database(con, schema, tables, cfg=DEFAULTS, db_id=None, log=print):
    """Profiles every listed table of one DuckDB schema. `con` is any DuckDB connection."""
    start = time.time()
    out = {}
    for table in sorted(tables):
        cols = columns(con, schema, table)
        out[table] = {
            "rows": con.execute(f"SELECT count(*) FROM {q(schema)}.{q(table)}").fetchone()[0],
            "columns": {c["name"]: profile_column(con, schema, table, c, cfg) for c in cols},
            "sample_rows": sample_rows(con, schema, table, cfg["sample_rows"], cfg["seed"]),
        }
    pairs = containment(con, schema, out, cfg)
    n_cols = sum(len(t["columns"]) for t in out.values())
    log(f"  profile {db_id or schema}: {len(out)} tables, {n_cols} columns, {len(pairs)} containment pairs, "
        f"{time.time() - start:.1f}s")
    return {"db_id": db_id or schema, "duckdb_schema": schema, "seed": cfg["seed"], "tables": out,
            "containment": pairs}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--in", dest="inp", help="default: out/filtered/<dataset>")
    ap.add_argument("--out", help="default: out/profiles/<dataset>")
    ap.add_argument("--databases", nargs="*", help="only these db_ids")
    ap.add_argument("--seed", type=int, default=DEFAULTS["seed"])
    ap.add_argument("--force", action="store_true", help="re-profile databases that already have a file")
    ap.add_argument("--overlaps", action="store_true", help="write out/overlaps from the existing profiles and stop")
    args = ap.parse_args()

    cfg = {**DEFAULTS, "seed": args.seed}
    inp = ROOT / (args.inp or f"out/filtered/{args.dataset}")
    out_dir = ROOT / (args.out or f"out/profiles/{args.dataset}")
    out_dir.mkdir(parents=True, exist_ok=True)

    for db in json.loads((inp / "databases.json").read_text()):
        if args.databases and db["db_id"] not in args.databases:
            continue
        path = out_dir / f"{db['db_id']}.json"
        if args.overlaps:
            pairs = partial_overlaps(open_db(db["duckdb_file"]), db["duckdb_schema"],
                                     json.loads(path.read_text())["tables"], cfg)
            target = ROOT / "out" / "overlaps" / args.dataset / f"{db['db_id']}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(pairs))
            print(f"  overlaps {db['db_id']}: {len(pairs)} pairs")
            continue
        if path.exists() and not args.force:
            print(f"  skip {db['db_id']} (exists, use --force)")
            continue
        prof = profile_database(open_db(db["duckdb_file"]), db["duckdb_schema"], db["tables"], cfg, db["db_id"])
        prof.update(dataset=db["dataset"], duckdb_file=db["duckdb_file"])
        path.write_text(json.dumps(prof, indent=1, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
