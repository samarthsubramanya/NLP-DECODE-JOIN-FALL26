"""Gold columns per question, parsed from the gold SQL.

Every column the query touches is resolved to its base table with sqlglot's
qualify + scope (handles aliases, unqualified columns, subqueries, CTEs) and
stored as a graph node id, which survives obfuscation unchanged.

Rules:
  *          SELECT * / COUNT(*) add no columns; the table still counts as gold
  roles      select, where, join, group, having, order (a column can have several)
  aliases    ORDER BY / HAVING on a SELECT alias resolves to nothing, not an error
  quotes     Spider and KaggleDBQA write string values in double quotes ("JetBlue");
             those are turned into string literals before parsing

Gold SQL is only used to build labels; it never enters profiling or prompts.
"""

import argparse
import json
import re
from collections import Counter

import sqlglot
import sqlglot.expressions as exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from baselines.joingr import ROOT, load_source_graph, DATASETS

GOLD = ROOT / "gold"

DIALECTS = {"beaver": "oracle", "bird": "sqlite", "spider": "sqlite", "kaggledbqa": "sqlite", "fiben": "postgres"}
# Beaver's oracle_sql differs from sql on 51/121 questions; JoinGR's gold tables follow sql.
SQL_FIELDS = ("sql", "oracle_sql", "query", "SQL")
DOUBLE_QUOTED_STRINGS = {"spider", "kaggledbqa"}

ROLE_OF = [(exp.Join, "join"), (exp.Where, "where"), (exp.Group, "group"), (exp.Having, "having"), (exp.Order, "order")]


def schema_index(g):
    """{db_id: {table_lower: {col_lower: node_id}}} plus {table_lower: canonical table key}."""
    by_db, tables = {}, {}
    for table_key, entry in g["table_node_map"]["by_table"].items():
        for nid in entry["all_nodes"]:
            node = g["nodes"][nid]
            db = node.get("db_id") or "dw"
            short = table_key.split(".", 1)[1] if table_key.startswith(f"{db}.") else table_key
            by_db.setdefault(db, {}).setdefault(short.lower(), {})[node["column_name"].lower()] = nid
            tables[(db, short.lower())] = table_key
    return by_db, tables


def _lowercase(tree):
    # Quoted and unquoted identifiers mix freely across datasets ("HASNAME", `CustomerID`);
    # matching is case-insensitive, so flatten everything before qualify sees it.
    for ident in tree.find_all(exp.Identifier):
        ident.set("this", ident.name.lower())
        ident.set("quoted", False)
    return tree


def _role(col):
    node = col.parent
    while node is not None and not isinstance(node, exp.Select):
        for node_type, role in ROLE_OF:
            if isinstance(node, node_type):
                return role
        node = node.parent
    return "select"


def _source(scope, table):
    # Correlated subqueries reference tables of an outer query (T1.id inside EXISTS).
    while scope is not None:
        if table in scope.sources:
            return scope.sources[table]
        scope = scope.parent
    return None


def _derived_output(scope, name):
    while scope is not None:
        for source in scope.sources.values():
            if isinstance(source, Scope) and name in {e.alias_or_name.lower() for e in getattr(source.expression, "selects", [])}:
                return True
        scope = scope.parent
    return False


def extract(sql, db, dialect, cols_by_table):
    schema = {t: {c: "text" for c in cols} for t, cols in cols_by_table.items()}
    tree = None
    for d in (dialect, None, "sqlite", "snowflake", "postgres"):
        try:
            tree = sqlglot.parse_one(sql, dialect=d)
            break
        except sqlglot.errors.SqlglotError:
            continue
    if tree is None:
        return None
    tree = _lowercase(tree)
    try:
        tree = qualify(tree, schema=schema, expand_stars=False, validate_qualify_columns=False,
                       quote_identifiers=False, identify=False)
    except sqlglot.errors.SqlglotError:
        pass

    found, roles, tables, unresolved = [], {}, [], []
    for scope in traverse_scope(tree):
        aliases = {s.alias_or_name.lower() for s in scope.expression.selects} if isinstance(scope.expression, exp.Select) else set()
        for source in scope.sources.values():
            if isinstance(source, exp.Table) and source.name in cols_by_table and source.name not in tables:
                tables.append(source.name)
        for col in scope.columns:
            source = _source(scope, col.table)
            if isinstance(source, exp.Table):
                nid = cols_by_table.get(source.name, {}).get(col.name)
                if nid:
                    if nid not in roles:
                        found.append(nid)
                        roles[nid] = []
                    role = _role(col)
                    if role not in roles[nid]:
                        roles[nid].append(role)
                    continue
            elif source is not None or (not col.table and _derived_output(scope, col.name)):
                continue  # column of a subquery/CTE; its base columns are picked up in the inner scope
            if col.name not in aliases:
                unresolved.append(f"{col.table + '.' if col.table else ''}{col.name}")
    return found, roles, tables, unresolved


def build(dataset):
    g = load_source_graph(dataset)
    by_db, table_keys = schema_index(g)
    dialect = DIALECTS[dataset]

    out, stats = [], Counter()
    for i, q in enumerate(g["queries"]):
        sql = next((q[f] for f in SQL_FIELDS if q.get(f)), "")
        if dataset in DOUBLE_QUOTED_STRINGS:
            sql = re.sub(r'"([^"]*)"', r"'\1'", sql)
        db = q.get("db_id") or "dw"
        result = extract(sql, db, dialect, by_db.get(db, {}))
        if result is None:
            stats["parse_failed"] += 1
            out.append({"i": i, "gold_columns": [], "gold_column_names": [], "column_roles": {}, "gold_tables": [],
                        "unresolved": [], "status": "parse_failed"})
            continue
        found, roles, tables, unresolved = result
        stats["ok"] += 1
        stats["with_unresolved"] += bool(unresolved)
        stats["no_columns"] += not found
        stats["columns"] += len(found)
        out.append({
            "i": i,
            "gold_columns": found,
            "gold_column_names": [g["nodes"][n]["node_name"] for n in found],
            "column_roles": {g["nodes"][n]["node_name"]: r for n, r in roles.items()},
            "gold_tables": [table_keys[(db, t)] for t in tables],
            "unresolved": unresolved,
            "status": "ok",
        })

    GOLD.mkdir(exist_ok=True)
    path = GOLD / f"{dataset}.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    n = len(out)
    print(f"{dataset}: {n} questions | parse failed {stats['parse_failed']} | no columns {stats['no_columns']} | "
          f"with unresolved refs {stats['with_unresolved']} | avg cols {stats['columns'] / max(stats['ok'], 1):.2f} -> {path}")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", nargs="+", default=list(DATASETS), choices=DATASETS)
    args = p.parse_args()
    for d in args.dataset:
        build(d)
