"""Evaluation labels and the obfuscated -> real name mapping, shared by grade_bird.py and
probe_join_recall.py. Labels come from the documented DuckDB file only (<dataset>_metadata.table_info,
SQLite's table_xinfo and foreign_key_list), never from a run's inputs. BIRD and Spider ship the same format.

    label_source  a stripped or obfuscated dataset (bird_stripped, bird_obfuscated) is graded on its
                  documented one (bird)
    mapping       {db: {"tables": {real: shown}, "columns": {real_table: {real: shown}}}}, or None when the
                  run shows real names
"""
import json
from collections import defaultdict
from pathlib import Path


def documented(dataset):
    """bird_stripped / bird_obfuscated -> bird; any other dataset is its own documented one."""
    return dataset.removesuffix("_obfuscated").removesuffix("_stripped")


def label_source(repo, dataset):
    """(metadata schema, {db_id: documented data schema}) for a dataset's labels."""
    base = documented(dataset)
    dbs = json.loads((Path(repo) / f"out/filtered/{base}/databases.json").read_text())
    return f"{base}_metadata", {d["db_id"]: d["duckdb_schema"] for d in dbs}


def position_mapping(con, schemas):
    """{db: {"tables": {t: t}, "columns": {t: {real: col_NNN}}}} from the documented file's column order."""
    out = {}
    for db, schema in schemas.items():
        cols = defaultdict(dict)
        for t, c, pos in con.execute("SELECT table_name, column_name, ordinal_position FROM information_schema.columns "
                                     "WHERE table_schema = ? ORDER BY table_name, ordinal_position", [schema]).fetchall():
            cols[t][c] = f"col_{pos:03d}"
        out[db] = {"tables": {t: t for t in cols}, "columns": dict(cols)}
    return out


def load_mapping(arg, con, schemas):
    """--mapping: "position", a mapping.json path, or None (names are real). schemas: {db: documented schema}."""
    if arg == "position":
        return position_mapping(con, schemas)
    return json.loads(Path(arg).read_text()) if arg else None


def back(mapping, db, t, c=None):
    """obfuscated -> real name (identity without a mapping)."""
    if not mapping:
        return (t, c) if c is not None else t
    m = mapping[db]
    rt = {v: k for k, v in m["tables"].items()}[t]
    return (rt, {v: k for k, v in m["columns"][rt].items()}[c]) if c is not None else rt


def norm_edge(mapping, db, e):
    """A join record or graph edge as an undirected pair of real, lower-case (table, column)."""
    s = back(mapping, db, e["source"]["table"], e["source"]["column"])
    t = back(mapping, db, e["target"]["table"], e["target"]["column"])
    return frozenset(((s[0].lower(), s[1].lower()), (t[0].lower(), t[1].lower())))


def gold(con, meta, schema):
    """Declared single- and multi-column primary keys, and declared FKs as ((table, col), (table, col)), lower-case."""
    pks, fks = {}, set()
    rows = con.execute(f'SELECT t.table_name, t.metadata_json FROM "{meta}".table_info t '
                       f'JOIN "{meta}".files f USING (source_path) WHERE f.duckdb_schema = ?', [schema]).fetchall()
    xinfo = {t.lower(): json.loads(m) for t, m in rows}
    for t, m in xinfo.items():
        pks[t] = [c["name"].lower() for c in sorted((c for c in m["table_xinfo"] if c["pk"]), key=lambda c: c["pk"])]
    for t, m in xinfo.items():
        for fk in m["foreign_key_list"]:
            tt = fk["table"].lower()
            to = (fk["to"] or "").lower()
            if not to and len(pks.get(tt, [])) > fk["seq"]:   # "references t" without a column = t's key
                to = pks[tt][fk["seq"]]
            if to:
                fks.add(((t, fk["from"].lower()), (tt, to)))
    return pks, fks
