"""Builds JoinGR-format graphs for datasets the JoinGR repo doesn't ship.

Follows JoinGR's own construction (spider_node_augmenter / spider_edge_augmenter):
a node per column, keys = PK columns plus both ends of every FK, intra edges from
each non-key column to the keys of its table, join edges from declared FKs.

Descriptions come from the dataset's own documentation, not an LLM:
  KaggleDBQA  normalized table/column names + human column descriptions
  FIBEN       none shipped, so its documented setting only adds keys and joins
Edge purposes stay empty; JoinGR wrote them with GPT-4o.
TODO(phase 3): fill column_purpose / edge_purpose with the frozen LLM once it's picked.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

import sqlglot
import sqlglot.expressions as exp

from baselines.joingr import ARTIFACTS, ROOT

RAW = ROOT.parent / "datasets"
SOURCE = ARTIFACTS / "source"


def node_id(name):
    return hashlib.sha256(name.encode()).hexdigest()[:16]


def edge_id(nid1, nid2):
    lo, hi = min(nid1, nid2), max(nid1, nid2)
    return hashlib.sha256(f"{lo}|{hi}".encode()).hexdigest()[:16]


# A schema here is {db_id: {"tables": {table: {"semantic": str, "columns": [col dict]}},
#                           "pks": {(table, col)}, "fks": [((table, col), (table, col))]}}

def load_kaggledbqa():
    entries = json.loads((RAW / "KaggleDBQA/KaggleDBQA_tables.json").read_text())
    schema = {}
    for e in entries:
        tables = {t: {"semantic": s, "columns": []} for t, s in zip(e["table_names_original"], e["table_names"])}
        cols = []
        for (t_idx, name), (_, sem), ctype, desc in zip(
            e["column_names_original"], e["column_names"], e["column_types"], e["column_descriptions"]
        ):
            if t_idx == -1:
                cols.append(None)
                continue
            table = e["table_names_original"][t_idx]
            tables[table]["columns"].append({"name": name, "semantic": sem, "purpose": desc, "type": ctype})
            cols.append((table, name))
        pks = set()
        for pk in e["primary_keys"]:
            pks.update(cols[i] for i in (pk if isinstance(pk, list) else [pk]))
        fks = [(cols[a], cols[b]) for a, b in e["foreign_keys"]]
        schema[e["db_id"]] = {"tables": tables, "pks": pks, "fks": fks}

    queries = []
    for db_id in schema:
        for q in json.loads((RAW / f"KaggleDBQA/examples/{db_id}.json").read_text()):
            queries.append({"db_id": db_id, "question": q["question"], "sql": q["query"]})
    return schema, queries, "sqlite"


CREATE_RE = re.compile(r"CREATE TABLE (\w+) \((.*)\);", re.I)
FK_RE = re.compile(r"ALTER TABLE (\w+) .*FOREIGN KEY \((\w+)\) REFERENCES (\w+) \((\w+)\)", re.I)


def load_fiben():
    tables, pks, fks = {}, set(), []
    for line in (RAW / "fiben-benchmark/FIBEN.sql").read_text().splitlines():
        if m := CREATE_RE.match(line.strip()):
            table, body = m.groups()
            pk = re.search(r"PRIMARY KEY \(([^)]*)\)", body)
            body = body[: pk.start()] if pk else body
            if pk:
                pks.update((table, c.strip()) for c in pk.group(1).split(","))
            columns = []
            for part in re.split(r",\s*(?![^()]*\))", body):
                part = part.strip().rstrip(",")
                if part:
                    name, ctype = part.split(" ", 1)
                    columns.append({"name": name, "semantic": "", "purpose": "", "type": ctype.replace(" NOT NULL", "")})
            tables[table] = {"semantic": "", "columns": columns}
        elif m := FK_RE.match(line.strip()):
            t1, c1, t2, c2 = m.groups()
            fks.append(((t1, c1), (t2, c2)))

    queries = [
        {"db_id": "fiben", "question": q["question"].strip(), "sql": q["SQL"].strip()}
        for q in json.loads((RAW / "fiben-benchmark/FIBEN_Queries.json").read_text())
    ]
    # Postgres is the closest sqlglot dialect to FIBEN's Db2 SQL (FETCH FIRST, quoted idents).
    return {"fiben": {"tables": tables, "pks": pks, "fks": fks}}, queries, "postgres"


LOADERS = {"kaggledbqa": load_kaggledbqa, "fiben": load_fiben}


def to_graph(schema):
    nodes, edges, by_table, by_node = {}, {}, {}, {}
    nid_of = {}

    for db_id, db in schema.items():
        keys = set(db["pks"]) | {c for fk in db["fks"] for c in fk}
        for table, t in db["tables"].items():
            table_key = f"{db_id}.{table}"
            all_nodes, key_nodes = [], []
            for c in t["columns"]:
                name = f"{db_id}.{table}.{c['name']}"
                nid = node_id(name)
                is_key = (table, c["name"]) in keys
                nodes[nid] = {
                    "node_id": nid, "node_name": name, "db_id": db_id,
                    "table_name": table_key, "semantic_table_name": t["semantic"],
                    "column_name": c["name"], "semantic_column_name": c["semantic"],
                    "column_purpose": c["purpose"], "column_type": c["type"], "is_key": is_key,
                }
                nid_of[(db_id, table, c["name"])] = nid
                all_nodes.append(nid)
                if is_key:
                    key_nodes.append(nid)
            by_table[table_key] = {"key_nodes": key_nodes, "all_nodes": all_nodes}
            for nid in all_nodes:
                by_node[nid] = {"table_name": table_key, "is_key": nid in key_nodes,
                                "key_nodes": key_nodes, "all_nodes": all_nodes}
            for nid in all_nodes:
                if nid in key_nodes:
                    continue
                for kid in key_nodes:
                    _add_edge(edges, "intra", nodes[nid], nodes[kid])

        for (t1, c1), (t2, c2) in db["fks"]:
            n1, n2 = nid_of.get((db_id, t1, c1)), nid_of.get((db_id, t2, c2))
            if n1 and n2:
                _add_edge(edges, "join", nodes[n1], nodes[n2])
            else:
                print(f"  [warn] FK {t1}.{c1} -> {t2}.{c2} points at a missing column")

    edge_to_nodes, node_to_edges = {}, {}
    for eid, e in edges.items():
        edge_to_nodes[eid] = {k: e[k] for k in ("node1_id", "node1_name", "node2_id", "node2_name", "edge_type", "edge_purpose")}
        node_to_edges.setdefault(e["node1_id"], []).append(eid)
        node_to_edges.setdefault(e["node2_id"], []).append(eid)

    return {
        "nodes": nodes, "edges": edges,
        "table_node_map": {"by_table": by_table, "by_node": by_node},
        "edge_to_nodes": edge_to_nodes, "node_to_edges": node_to_edges,
    }


def _add_edge(edges, etype, n1, n2):
    eid = edge_id(n1["node_id"], n2["node_id"])
    if eid in edges:
        return
    edges[eid] = {
        "edge_id": eid, "edge_type": etype, "edge_purpose": "",
        "node1_id": n1["node_id"], "node1_name": n1["node_name"],
        "node1_semantic_table_name": n1["semantic_table_name"],
        "node1_semantic_column_name": n1["semantic_column_name"],
        "node2_id": n2["node_id"], "node2_name": n2["node_name"],
        "node2_semantic_table_name": n2["semantic_table_name"],
        "node2_semantic_column_name": n2["semantic_column_name"],
    }


FROM_RE = re.compile(r'(?:FROM|JOIN)\s+(?:\w+\.)?"?(\w+)"?', re.I)


def gold_tables(sql, db_id, dialect, canonical):
    """Base tables a query reads, in the graph's own casing. CTE names are dropped.
    Falls back to a FROM/JOIN regex when sqlglot can't parse the query.
    TODO(task 3): extend to gold columns; this only needs tables for now."""
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        names, parsed = [t.name for t in tree.find_all(exp.Table)], True
    except sqlglot.errors.SqlglotError:
        ctes, names, parsed = set(), FROM_RE.findall(sql), False
    found, unknown = [], []
    for name in names:
        if name.lower() in ctes:
            continue
        key = canonical.get(f"{db_id}.{name}".lower())
        if key is None:
            unknown.append(name)
        elif key not in found:
            found.append(key)
    return found, unknown, parsed


def build(dataset):
    schema, raw_queries, dialect = LOADERS[dataset]()
    g = to_graph(schema)
    canonical = {t.lower(): t for t in g["table_node_map"]["by_table"]}

    queries, fallback, unknown = [], 0, 0
    for i, q in enumerate(raw_queries):
        gold, missing, parsed = gold_tables(q["sql"], q["db_id"], dialect, canonical)
        fallback += not parsed
        unknown += len(missing)
        if gold:
            queries.append({"question_id": i, "db_id": q["db_id"], "question": q["question"],
                            "query": q["sql"], "gold_tables": gold})
    g["queries"] = queries

    out = SOURCE / dataset
    out.mkdir(parents=True, exist_ok=True)
    for name, obj in g.items():
        (out / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False))

    n_join = sum(e["edge_type"] == "join" for e in g["edges"].values())
    print(f"{dataset}: {len(schema)} dbs, {len(g['table_node_map']['by_table'])} tables, {len(g['nodes'])} nodes, "
          f"{len(g['edges'])} edges ({n_join} join) | {len(queries)}/{len(raw_queries)} queries kept, "
          f"{fallback} regex fallbacks, {unknown} unknown table refs -> {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", nargs="+", default=list(LOADERS), choices=LOADERS)
    args = p.parse_args()
    for d in args.dataset:
        build(d)
