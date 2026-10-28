"""Step 4: record the declared keys that stripped removes, as evaluation gold.

Reads the declared primary and foreign keys from the original SQLite files and writes
them to JSON, keyed by the DuckDB schema names from schema_mapping.csv, so they line up
with the DuckDB tables. This file is EVALUATION ONLY: keep it out of any folder or prompt
the pipeline can read.

Usage (BIRD):
    python3 extract_gold_keys.py --data-dir /path/to/dev_20240627 \
        --mapping NLP-DECODE-JOIN-FALL26/data/bird/schema_mapping.csv \
        --out bird_dev_gold_keys.json

--data-dir is the folder that contains the source_path values from the mapping
(for BIRD, the one holding dev_databases/).
"""
import argparse
import csv
import json
import sqlite3
from pathlib import Path


def q(name):
    return '"' + name.replace('"', '""') + '"'


def read_sqlite(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    info = {}
    for t in tables:
        cols = con.execute(f"PRAGMA table_info({q(t)})").fetchall()
        # (cid, name, type, notnull, dflt, pk) ; pk is 1-based position inside the key
        pk = [c[1] for c in sorted((c for c in cols if c[5] > 0), key=lambda c: c[5])]
        info[t] = {"columns": [{"name": c[1], "type": c[2]} for c in cols], "primary_key": pk}
    fks, unresolved = [], []
    for t in tables:
        groups = {}
        for r in con.execute(f"PRAGMA foreign_key_list({q(t)})").fetchall():
            # (id, seq, table, from, to, on_update, on_delete, match)
            groups.setdefault(r[0], []).append(r)
        for rows in groups.values():
            rows.sort(key=lambda r: r[1])
            dst_t = rows[0][2]
            src_cols = [r[3] for r in rows]
            dst_cols = [r[4] for r in rows]
            if any(c is None for c in dst_cols):  # FK to the target's primary key
                pk = info.get(dst_t, {}).get("primary_key", [])
                if len(pk) == len(src_cols):
                    dst_cols = pk
                else:
                    unresolved.append({"src_table": t, "src_cols": src_cols, "dst_table": dst_t})
                    continue
            fks.append({"src_table": t, "src_cols": src_cols, "dst_table": dst_t,
                        "dst_cols": dst_cols, "composite": len(src_cols) > 1})
    con.close()
    return info, fks, unresolved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--mapping", required=True, help="schema_mapping.csv (source_path,duckdb_schema)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    root = Path(a.data_dir).expanduser()
    out, edges = {}, []
    print(f"{'database':45} {'tables':>6} {'with PK':>8} {'FKs':>5} {'composite':>9}")
    with open(a.mapping, newline="") as fh:
        for row in csv.DictReader(fh):
            sp, schema = row["source_path"], row["duckdb_schema"]
            path = root / sp
            if not path.exists():
                print(f"MISSING: {path}")
                continue
            info, fks, unresolved = read_sqlite(path)
            for fk in fks:
                if not fk["composite"]:
                    edges.append({"schema": schema, "src_table": fk["src_table"],
                                  "src_col": fk["src_cols"][0], "dst_table": fk["dst_table"],
                                  "dst_col": fk["dst_cols"][0]})
            out[schema] = {
                "source_path": sp, "tables": info, "foreign_keys": fks, "unresolved_fks": unresolved,
                "tables_without_primary_key": [t for t, v in info.items() if not v["primary_key"]],
                "composite_primary_keys": [t for t, v in info.items() if len(v["primary_key"]) > 1],
            }
            n_pk = sum(1 for v in info.values() if v["primary_key"])
            n_comp = sum(1 for f in fks if f["composite"])
            print(f"{Path(sp).stem[:45]:45} {len(info):>6} {n_pk:>8} {len(fks):>5} {n_comp:>9}")

    payload = {"note": "EVALUATION ONLY. Declared keys removed by the stripped setting.",
               "databases": out, "single_column_fk_edges": edges}
    Path(a.out).write_text(json.dumps(payload, indent=1))
    print(f"\n{len(out)} databases, {len(edges)} single-column FK edges -> {a.out}")
    print("Check 'tables_without_primary_key' in the JSON: incomplete declared keys bias evaluation.")


if __name__ == "__main__":
    main()
