"""Stage 4: one refinement pass, then export to JoinGR's graph format.

Reads Stage 3's output (out/descriptions/<dataset>/<stage3-run>/: descriptions and joins/ with graphs.llm)
and writes, per database:

    out/joingr/<dataset>/<run>/<db_id>.json            the JoinGR graph (nodes, edges, table_node_map, ...)
    out/joingr/<dataset>/<run>/<db_id>.refined.json    the descriptions after refinement
    out/joingr/<dataset>/<run>/refine_log.jsonl        one line per column the refinement changed
    out/joingr/<dataset>/<run>/llm_calls.jsonl         one line per LLM call

Refinement (proposal, Stage 4): one pass, one LLM call per table that has an accepted join and a column
marked tentative or unresolved. The model sees the table's current descriptions and, for every column
with an accepted join, the description of the column on the other end and that table's purpose. It may
rewrite only the tentative / unresolved columns of that table. It never sees rejected or abstained joins
and cannot add or drop a join: the exported edges are exactly graphs.llm (accepted + llm_accept).

Export follows JoinGR's own construction (poc/export.py): a node per column, keys = the Stage 2a primary
key plus both ends of every exported join, intra edges from each non-key column to its table's keys,
join edges for the accepted joins. Semantic names and purposes come from the (refined) descriptions;
an unresolved column keeps them empty. Each join edge gets an edge_purpose built from the two ends'
descriptions (no LLM), which JoinGR embeds with the edge.

    python src/export_joingr.py --dataset bird --stage3-run dist_low --run full
    python src/export_joingr.py --dataset bird --stage3-run dist_low --run no_refine --no-refine
    python src/export_joingr.py --dataset bird --stage3-run dist_low --run joins_only --no-descriptions
    python src/export_joingr.py --dataset bird --stage3-run dist_low --run desc_only --no-joins
"""
import argparse
import copy
import json
import sys
from concurrent.futures import ThreadPoolExecutor

from describe_columns import DEFAULTS as S3, STATUSES, Voyager, clip, pack
from primary_keys import ROOT, load_profile, work_list

sys.path.insert(0, str(ROOT))
from poc import export   # noqa: E402  (JoinGR graph construction, shared with the PoC)

REFINE = """You revise the documentation of one database table, once, using its joins.
You get the table's current column descriptions, each marked resolved, tentative or unresolved, and
for every column that has an accepted join, the documented column on the other end of the join and
what that other table is for. The joins are established; do not question them.

Revise only the columns listed under "revise". For each, use the joined neighbours as extra evidence:
a column that references another table's key usually identifies a row of that table; a key that is
referenced by others is what they point at. Keep a description that is already right. Do not guess:
if the evidence still does not support a meaning, keep the column unresolved with empty name and
description.

status:
  resolved   the evidence clearly supports the meaning
  tentative  plausible, but another reading fits too; say which in the description
  unresolved not enough evidence; leave name and description empty

Answer with JSON only, in the given schema, one entry per column under its exact key."""


def refine_schema(columns):
    col = {"type": "object", "additionalProperties": False, "required": ["name", "description", "status"],
           "properties": {"name": {"type": "string"}, "description": {"type": "string"},
                          "status": {"type": "string", "enum": list(STATUSES)}}}
    return {"name": "table_refinement", "strict": True, "schema": {
        "type": "object", "additionalProperties": False, "required": list(columns),
        "properties": {c: col for c in columns}}}


def neighbours(edges, desc):
    """{(table, column): [the other end of each accepted join, documented]}."""
    def end(t, c):
        td = desc["tables"].get(t, {})
        cd = td.get("columns", {}).get(c, {})
        return {"table": t, "column": c, "table_purpose": td.get("table", {}).get("purpose", ""),
                "name": cd.get("name", ""), "description": cd.get("description", ""), "status": cd.get("status", "")}
    out = {}
    for e in edges:
        s, t = e["source"], e["target"]
        out.setdefault((s["table"], s["column"]), []).append({"direction": "references", **end(t["table"], t["column"])})
        out.setdefault((t["table"], t["column"]), []).append({"direction": "referenced_by", **end(s["table"], s["column"])})
    return out


BATCH_REFINE = REFINE + """

This request holds several tables under "tables". Revise each one on its own by the rules above, one entry per table
under its exact key."""


def refine_payload(table, desc, links):
    td = desc["tables"][table]
    todo = [c for c, cd in td["columns"].items() if cd["status"] != "resolved"]
    return todo, {
        "table": table, "table_purpose": td["table"].get("purpose", ""),
        "columns": {c: {k: cd.get(k, "") for k in ("role", "name", "description", "status")}
                    | ({"joins": links[(table, c)]} if (table, c) in links else {})
                    for c, cd in td["columns"].items()},
        "revise": todo,
    }


def refine_table(llm, table, desc, links, cfg):
    todo, payload = refine_payload(table, desc, links)
    reply, _ = llm.complete("refine", table, REFINE, json.dumps(payload, default=lambda v: clip(str(v), 80)),
                            refine_schema(todo))
    return todo, reply or {}


def refine_batch(llm, tables, desc, links, cfg):
    """{table: (todo, reply)} for several tables in one call. A table the reply leaves out is asked on its own."""
    if len(tables) == 1:
        return {tables[0]: refine_table(llm, tables[0], desc, links, cfg)}
    parts = {t: refine_payload(t, desc, links) for t in tables}
    schema = {"name": "table_refinement_batch", "strict": True, "schema": {
        "type": "object", "additionalProperties": False, "required": ["tables"],
        "properties": {"tables": {"type": "object", "additionalProperties": False, "required": tables,
                                  "properties": {t: refine_schema(p[0])["schema"] for t, p in parts.items()}}}}}
    reply, _ = llm.complete("refine", f"batch of {len(tables)} tables", BATCH_REFINE,
                            json.dumps({"tables": {t: p[1] for t, p in parts.items()}},
                                       default=lambda v: clip(str(v), 80)), schema)
    got = (reply or {}).get("tables") or {}
    return {t: (parts[t][0], got[t]) if got.get(t) else refine_table(llm, t, desc, links, cfg) for t in tables}


def refine(llm, desc, edges, cfg):
    """One pass. Returns (refined descriptions, log rows). Resolved columns are never touched."""
    out, log = copy.deepcopy(desc), []
    links = neighbours(edges, desc)
    joined = {t for t, _ in links}
    tables = [t for t in desc["tables"] if t in joined
              and any(cd["status"] != "resolved" for cd in desc["tables"][t]["columns"].values())]
    chunks = pack(tables, lambda t: len(desc["tables"][t]["columns"]), cfg.get("batch_columns", 0))
    with ThreadPoolExecutor(cfg["workers"]) as pool:
        results = {t: r for part in pool.map(lambda c: refine_batch(llm, c, desc, links, cfg), chunks)
                   for t, r in part.items()}
    for t, (todo, reply) in results.items():
        for c in todo:
            new, old = reply.get(c) or {}, out["tables"][t]["columns"][c]
            status = new.get("status") if new.get("status") in STATUSES else old["status"]
            if status != "unresolved" and not (new.get("description") or "").strip():
                status = old["status"]
                new = old
            keep = status != "unresolved"
            upd = {**old, "name": new.get("name", "") if keep else "", "description": new.get("description", "") if keep else "",
                   "status": status}
            if (upd["name"], upd["description"], upd["status"]) != (old["name"], old["description"], old["status"]):
                log.append({"table": t, "column": c, "before": {k: old[k] for k in ("name", "description", "status")},
                            "after": {k: upd[k] for k in ("name", "description", "status")}})
            out["tables"][t]["columns"][c] = upd
    return out, log, len(tables)


def edge_purpose(e, desc):
    def say(side):
        td = desc["tables"].get(side["table"], {}) if desc else {}
        cd = td.get("columns", {}).get(side["column"], {})
        name = cd.get("name") or side["column"]
        what = cd.get("description") or td.get("table", {}).get("purpose", "")
        return f"{td.get('table', {}).get('name') or side['table']}.{name}" + (f" ({what})" if what else "")
    card = "one-to-one" if e.get("cardinality") == "one_to_one" else "many-to-one"
    return f"{say(e['source'])} references {say(e['target'])}; {card} join."


def export_db(dataset, db, s3_dir, desc, cfg, args):
    joins = json.loads((s3_dir / "joins" / f"{db['db_id']}.json").read_text())
    keys = json.loads((ROOT / "out" / "keys" / dataset / joins["keys_run"] / f"{db['db_id']}.json").read_text())
    profile = load_profile(dataset, db)
    graph = "none" if args.no_joins else "llm"
    variant = graph + ("" if args.no_descriptions else "+desc")
    g = export.build({db["db_id"]: {"profiles": profile, "keys": keys, "joins": joins, "descriptions": desc}},
                     variant, {"export": {"table_key": "{db}.{table}"}})
    if graph == "llm":
        by_pair = {}
        for e in joins["graphs"]["llm"]:
            a = export.node_id(f"{db['db_id']}.{e['source']['table']}.{e['source']['column']}")
            b = export.node_id(f"{db['db_id']}.{e['target']['table']}.{e['target']['column']}")
            by_pair[export.edge_id(a, b)] = edge_purpose(e, None if args.no_descriptions else desc)
        for eid, text in by_pair.items():
            g["edges"][eid]["edge_purpose"] = g["edge_to_nodes"][eid]["edge_purpose"] = text
    return g, joins


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--databases", nargs="*")
    ap.add_argument("--stage3-run", required=True, help="out/descriptions/<dataset>/<run> to read")
    ap.add_argument("--run", default="default", help="output sub-folder of out/joingr/<dataset>/")
    ap.add_argument("--no-refine", action="store_true", help="export Stage 3's descriptions as they are")
    ap.add_argument("--no-descriptions", action="store_true", help="joins only: no semantic names or purposes")
    ap.add_argument("--no-joins", action="store_true", help="descriptions only: no join edges")
    ap.add_argument("--model", default=S3["model"])
    ap.add_argument("--reasoning-effort", default="low", choices=["none", "low", "medium", "high"])
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--batch-columns", type=int, default=0,
                    help="pack small tables into one refine call up to this many columns; 0: one call per table")
    args = ap.parse_args()
    cfg = {**S3, "model": args.model, "reasoning_effort": args.reasoning_effort, "workers": args.workers,
           "batch_columns": args.batch_columns}

    s3_dir = ROOT / "out" / "descriptions" / args.dataset / args.stage3_run
    out_dir = ROOT / "out" / "joingr" / args.dataset / args.run
    out_dir.mkdir(parents=True, exist_ok=True)
    llm = Voyager(cfg, out_dir / "llm_calls.jsonl")
    log_path = out_dir / "refine_log.jsonl"
    log_path.write_text("")
    for db in work_list(args):
        desc = None
        if not args.no_descriptions:
            desc = json.loads((s3_dir / f"{db['db_id']}.json").read_text())
            if not args.no_refine and not args.no_joins:
                joins = json.loads((s3_dir / "joins" / f"{db['db_id']}.json").read_text())
                desc, log, n_tables = refine(llm, desc, joins["graphs"]["llm"], cfg)
                with log_path.open("a") as f:
                    for row in log:
                        f.write(json.dumps({"db_id": db["db_id"], **row}, ensure_ascii=False) + "\n")
                desc["refined"] = {"tables_sent": n_tables, "columns_changed": len(log)}
            (out_dir / f"{db['db_id']}.refined.json").write_text(json.dumps(desc, indent=1, ensure_ascii=False))
        g, joins = export_db(args.dataset, db, s3_dir, desc, cfg, args)
        (out_dir / f"{db['db_id']}.json").write_text(json.dumps(g, ensure_ascii=False))
        n_join = sum(e["edge_type"] == "join" for e in g["edges"].values())
        note = f", refined {desc['refined']}" if desc and "refined" in desc else ""
        print(f"  {db['db_id']}: {len(g['nodes'])} nodes, {n_join} join edges{note}")


if __name__ == "__main__":
    main()
