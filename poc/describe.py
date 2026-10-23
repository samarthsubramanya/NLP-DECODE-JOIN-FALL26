"""Stage 3: column descriptions, one LLM call per table, after the final join graph.

Each column gets a role, a short readable name, a one-line description and a status
(resolved / tentative / unresolved). The model is told not to guess. Columns that
point at a small lookup table get a few of its code -> label pairs as evidence.
"""

import json

from poc.catalog import q
from poc.config import POC
from poc.evidence import column_summary, sample_rows

SYSTEM = (POC / "prompts" / "describe.md").read_text()
STATUSES = ("resolved", "tentative", "unresolved")


def final_graph(joins):
    return joins["graphs"].get("llm") or joins["graphs"]["stats"]


def lookup_pairs(cat, profiles, edge, cfg):
    t = edge["target"]["table"]
    tp = profiles["tables"][t]
    if tp["rows"] > cfg["describe"]["lookup_max_rows"]:
        return None
    labels = [c for c, p in tp["columns"].items() if p["role"] in ("text", "code", "free_text") and c != edge["target"]["column"]]
    if not labels:
        return None
    key, label = q(edge["target"]["column"]), q(labels[0])
    rows = cat.rows(f"SELECT {key}, {label} FROM {q(t)} ORDER BY {key} LIMIT {int(cfg['describe']['lookup_pairs'])}")
    return [[str(k), str(v)] for k, v in rows]


def payload(cat, table, profiles, keys, edges, cfg):
    use_names, use_values = keys["use_names"], cfg["describe"]["use_values"]
    tp, tk = profiles["tables"][table], keys["tables"][table]
    columns = {}
    for name, prof in tp["columns"].items():
        col = column_summary(prof, use_names, use_values)
        col["is_primary_key"] = tk["primary_key"] == name
        col["joins"] = []
        for e in edges:
            for here, there in (("source", "target"), ("target", "source")):
                if e[here]["table"] == table and e[here]["column"] == name:
                    link = {"to": f"{e[there]['table']}.{e[there]['column']}", "direction": "references" if here == "source" else "referenced_by"}
                    if here == "source" and use_values:
                        pairs = lookup_pairs(cat, profiles, e, cfg)
                        if pairs:
                            link["lookup_pairs"] = pairs
                    col["joins"].append(link)
        columns[name] = col
    return {"table": table, "rows": tp["rows"], "columns": columns, "sample_rows": sample_rows(tp, cfg["profile"]["sample_rows"], use_values)}


def run(cat, profiles, keys, joins, cfg, llm, log=print):
    edges = final_graph(joins)
    tables, counts = {}, {s: 0 for s in STATUSES}
    for table, tp in profiles["tables"].items():
        reply = llm.complete_json("describe", SYSTEM, json.dumps(payload(cat, table, profiles, keys, edges, cfg), default=str)) or {}
        got = reply.get("columns") or {}
        columns = {}
        for name in tp["columns"]:
            c = got.get(name) or {}
            status = c.get("status") if c.get("status") in STATUSES else "unresolved"
            columns[name] = {
                "role": c.get("role", ""),
                "name": c.get("name", "") if status != "unresolved" else "",
                "description": c.get("description", "") if status != "unresolved" else "",
                "status": status,
            }
            counts[status] += 1
        t = reply.get("table") or {}
        tables[table] = {"table": {"name": t.get("name", ""), "purpose": t.get("purpose", "")}, "columns": columns}
    log(f"  describe: {counts}")
    return {"db_id": profiles["db_id"], "use_names": keys["use_names"], "use_values": cfg["describe"]["use_values"], "tables": tables}
