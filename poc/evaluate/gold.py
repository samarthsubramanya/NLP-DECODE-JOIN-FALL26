"""Ground truth for evaluation, translated into the evaluated setting's names.
Nothing here is ever visible to the pipeline."""

import csv
import json

from poc.catalog import Catalog, find_databases
from poc.config import resolve


def load_mapping(cfg, setting):
    """{"db.table.col" (original, lowercased): "db.table.col" (setting)}; identity when no file."""
    path = resolve(cfg, cfg["evaluate"]["mapping"], setting=setting)
    if not path.exists():
        return {}
    return {k.lower(): v for k, v in json.loads(path.read_text()).items()}


class Names:
    """Translates (db, table, column) between original and setting names, case-insensitively."""

    def __init__(self, mapping):
        self.fwd = mapping
        self.back = {v.lower(): k for k, v in mapping.items()}

    def to_setting(self, db, table, col):
        key = f"{db}.{table}.{col}".lower()
        return tuple(self.fwd.get(key, key).lower().split(".", 2))

    def to_original(self, db, table, col):
        key = f"{db}.{table}.{col}".lower()
        return tuple(self.back.get(key, key).lower().split(".", 2))


def keys_from_tables_json(cfg):
    """{db: {"pks": {(table, col)}, "composite": n, "fks": {((t, c), (t, c))}}} in original names."""
    entries = json.loads(resolve(cfg, cfg["evaluate"]["tables_json"]).read_text())
    gold = {}
    for e in entries:
        cols = [(e["table_names_original"][t], c) if t >= 0 else None for t, c in e["column_names_original"]]
        pks, composite = set(), 0
        for pk in e["primary_keys"]:
            if isinstance(pk, list):
                composite += 1
            else:
                pks.add(cols[pk])
        fks = {(cols[a], cols[b]) for a, b in e["foreign_keys"]}
        gold[e["db_id"]] = {"pks": pks, "composite": composite, "fks": fks}
    return gold


def keys_from_duckdb(cfg):
    gold = {}
    for db, path in find_databases(cfg, cfg["evaluate"]["documented_setting"]).items():
        cat = Catalog(path, db)
        try:
            pks, fks = cat.declared_keys()
        finally:
            cat.close()
        gold[db] = {"pks": set(pks), "composite": 0, "fks": set(fks)}
    return gold


KEY_SOURCES = {"tables_json": keys_from_tables_json, "duckdb": keys_from_duckdb}


def gold_keys(cfg, names):
    """Gold keys in the setting's names, lowercased for matching."""
    out = {}
    for db, g in KEY_SOURCES[cfg["evaluate"]["keys_gold"]](cfg).items():
        tr = lambda tc: names.to_setting(db, *tc)[1:]
        out[db.lower()] = {
            "pks": {tr(x) for x in g["pks"]},
            "composite": g["composite"],
            "fks": {(tr(a), tr(b)) for a, b in g["fks"]},
        }
    return out


def reference_descriptions(cfg, db):
    """{(table, column) original lowercased: text} from BIRD's database_description CSVs, if present."""
    folder = resolve(cfg, cfg["evaluate"]["reference_descriptions"]) / db / "database_description"
    if not folder.exists():
        return {}
    refs = {}
    for path in folder.glob("*.csv"):
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        for row in csv.DictReader(text.splitlines()):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            col = row.get("original_column_name")
            if not col:
                continue
            parts = [row.get("column_name", ""), row.get("column_description", ""), row.get("value_description", "")]
            refs[(path.stem.lower(), col.lower())] = " ".join(p for p in parts if p)
    return refs
