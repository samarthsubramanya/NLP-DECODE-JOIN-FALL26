"""Memorization probe: how many hidden column names the LLM writes back from col_NNN.

Reads Stage 3 descriptions of an obfuscated run and compares each column's proposed "name" to the real
name in the documented DuckDB schema (position-based col_NNN, or the run's mapping.json if it has one).

    python evals/probe_name_recovery.py <dataset> [run]      e.g. spider ddb_low

A name counts as recovered if it is equal after lowercasing and dropping spaces and underscores: the
prompt asks for a readable snake_case name, so "County Name" comes back as county_name. This reproduces
the 513 of bird_obfuscated/obfuscated_low reported in docs/stage3_summary.md (over 757 columns not marked
unresolved). Recovery alone does not separate memory from inference (birth_year from 1940-1962 values),
so read the per-database hits: idiosyncratic names (yearid, stat_cause_descr, GNPOld) mean memory.
"""
import json
import re
import sys
from pathlib import Path

Q = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(Q / "src"), str(Q / "evals")]
import labels                                   # noqa: E402
from duckdb_driver import open_db               # noqa: E402


def norm(s):
    return re.sub(r"[\s_]", "", s.lower())


def probe(dataset, run="ddb_low"):
    dbs = json.loads((Q / f"out/filtered/{dataset}/databases.json").read_text())
    out = Q / f"out/descriptions/{dataset}_obfuscated/{run}"
    real = labels.load_mapping(str(out / "mapping.json") if (out / "mapping.json").exists() else "position",
                               open_db(dbs[0]["duckdb_file"]), {d["db_id"]: d["duckdb_schema"] for d in dbs})
    per_db = {}
    for f in sorted(out.glob("*.json")):
        if f.name in ("mapping.json", "grade.json"):
            continue
        db = json.loads(f.read_text())
        m = real[db["db_id"]]["columns"]
        by_lower = {t.lower(): t for t in m}
        x = per_db.setdefault(db["db_id"], {"columns": 0, "resolved": 0, "recovered": 0, "hits": []})
        for t, td in db["tables"].items():
            shown = {v: k for k, v in m[by_lower[t.lower()]].items()}       # col_NNN -> real
            for c, cd in td.get("columns", {}).items():
                r = shown[c]
                x["columns"] += 1
                x["resolved"] += cd.get("status") != "unresolved"
                if norm(cd.get("name") or "") == norm(r):
                    x["recovered"] += 1
                    x["hits"].append(f"{t}.{r}")
    return per_db


if __name__ == "__main__":
    per_db = probe(*sys.argv[1:3])
    tot = {k: sum(x[k] for x in per_db.values()) for k in ("columns", "resolved", "recovered")}
    for db, x in per_db.items():
        print(f"{db:32} {x['recovered']:5}/{x['columns']:<5} e.g. {', '.join(x['hits'][:6])}")
    print(f"TOTAL recovered {tot['recovered']}/{tot['columns']} ({100 * tot['recovered'] / tot['columns']:.0f}%)"
          f" | of resolved {tot['recovered']}/{tot['resolved']} ({100 * tot['recovered'] / tot['resolved']:.0f}%)")
