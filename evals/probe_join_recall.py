"""Join recall probe: how much of a Stage 3 run's join adjudication the model could have done from memory.

Stage 3 shows the adjudicator real table names even in the obfuscated setting, and BIRD is public, so
a correct verdict may come from recalling the schema rather than reading the evidence. Three checks on
one finished Stage 3 run, with the run's own model and reasoning effort (temperature 0, cached):

    leak scan     no LLM. Real column names that the run hid (obfuscated runs only) found in the
                  verdict reasons. Only identifier-shaped names count (snake_case, camelCase or with a
                  digit, e.g. PostId, district_id, A2) so that words read off the values do not.
    open recall   one call per database. The model gets the table and column names exactly as the run
                  showed them, no db_id, no values, no statistics, and lists the joins it believes
                  exist. Scored against BIRD's declared FKs as undirected table pairs and column pairs,
                  next to chance (gold table pairs / all table pairs, self pairs included). Negative
                  control: the same call with tables renamed tbl_NN and columns col_NNN in profile
                  order; it should land near chance, else the prompt leaks something.
    blind         one call per band record of the run. The adjudication question with only the two
                  table.column names. Each record's full and blind verdicts are crossed by class
                  (gold / not gold); a verdict is correct when it accepts a gold join or rejects any
                  other, abstain is never correct.

    memory_reachable_share = full-correct records the blind probe also gets right / full-correct records
    evidence_share         = 1 - memory_reachable_share
A blind-correct record shows memory was enough, not that the full verdict used it, so the share is an
upper bound on memory reliance. Report it per class: ~87% of band records are not gold, so rejecting
everything already scores well pooled.

Labels (declared FKs) are read only here, from the documented DuckDB file (evals/labels.py), as in grade_bird.py.

Usage (from the repo root):
    python evals/probe_join_recall.py . <documented.duckdb> <stage3_dir> [--dataset D] [--mapping M] [--databases ...]
    python evals/probe_join_recall.py . ~/Downloads/decode_join_bird_dev.duckdb out/descriptions/bird/names
    python evals/probe_join_recall.py . ~/Downloads/decode_join_bird_dev.duckdb \\
        out/descriptions/bird_obfuscated/obfuscated_low --dataset bird_obfuscated --mapping position
    python evals/probe_join_recall.py . ~/Downloads/decode_join.duckdb out/descriptions/spider/names --dataset spider
<documented.duckdb> holds the labels: decode_join_bird_dev.duckdb for bird and bird_obfuscated, decode_join.duckdb
for spider.
--dataset, --mapping as in grade_bird.py. Only databases whose <stage3_dir>/joins/<db>.json exists are probed.
--dry-run   no LLM calls: the leak scan, the record counts and one payload of each kind.

Output: <stage3_dir>/probe/probe.json (summary), open_recall.json, blind_verdicts.jsonl, llm_calls.jsonl
"""
import argparse
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb

import labels as bl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from describe_columns import DEFAULTS, VERDICTS, VERDICT_SCHEMA, Voyager   # noqa: E402

OPEN_RECALL = """You are given the table names and column names of a database, nothing else: no values,
no statistics.

List every foreign-key join you believe exists between its tables: a column of one table whose
values reference the key of another table (or of the same table). Use the table and column names
exactly as given. When you believe two tables join but do not know through which columns, leave
both column fields as "". If you cannot tell, return an empty list; do not invent joins.

Answer with JSON only: {"joins": [{"from_table": "...", "from_column": "...", "to_table": "...", "to_column": "..."}]}"""

OPEN_SCHEMA = {"name": "join_list", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["joins"],
    "properties": {"joins": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["from_table", "from_column", "to_table", "to_column"],
        "properties": {k: {"type": "string"} for k in ("from_table", "from_column", "to_table", "to_column")}}}}}}

# ADJUDICATE without the evidence: "matching names alone are not enough" is left out, since names are
# all the model gets here and the rule would only force abstain
BLIND_ADJUDICATE = """You review one candidate join between two columns of a database. You get only the
table and column names: no values, no statistics.

Decide whether the source column really references the target column, i.e. whether each source
value names the same real-world entity as the matching target value. You may only judge this
candidate; do not suggest other joins.

accept   you have good reason to believe the source column references the target column
reject   you have good reason to believe it does not
abstain  you cannot tell

Answer with JSON only: {"decision": "accept" | "reject" | "abstain", "reason": "<one sentence>"}"""

IDENTIFIER = re.compile(r"_|\d|[a-z][A-Z]")   # snake_case, digits or camelCase


def prf(tp, pred, gold_n):
    p = tp / pred if pred else 0.0
    r = tp / gold_n if gold_n else 0.0
    return {"tp": tp, "predicted": pred, "gold": gold_n, "precision": round(p, 4), "recall": round(r, 4),
            "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0}


def share(a, b):
    return round(a / b, 4) if b else None


# ------------------------------------------------------------------ leak scan
def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def hidden_names(mapping, db):
    """Real column names the run replaced, identifier-shaped and not guessable from a visible table name
    (the table itself, <table>_id or <singular table>_id)."""
    if not mapping:
        return set()
    m = mapping[db]
    guessable = {f for t in m["tables"] for f in (norm(t), norm(t) + "id", norm(t).rstrip("s") + "id")}
    return {real for cols in m["columns"].values() for real, shown in cols.items()
            if real != shown and IDENTIFIER.search(real) and norm(real) not in guessable}


def leaks(text, names):
    return sorted(n for n in names
                  if re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", text or "", re.I))


# ------------------------------------------------------------------ open recall
def shown_schema(profile):
    """{table: [column, ...]} as the run showed them."""
    return {t: list(tp["columns"]) for t, tp in profile["tables"].items()}


def opaque(schema):
    """Negative control: tbl_NN / col_NNN in profile order. Returns the renamed schema and {(tbl, col): (table, column)}."""
    renamed, back = {}, {}
    for i, (t, cols) in enumerate(schema.items(), 1):
        o = f"tbl_{i:02d}"
        renamed[o] = [f"col_{j:03d}" for j in range(1, len(cols) + 1)]
        back[(o, None)] = (t, None)
        back.update({(o, f"col_{j:03d}"): (t, c) for j, c in enumerate(cols, 1)})
    return renamed, back


def score_open(reply_joins, schema, back_to_run, mapping, db, fks):
    """Undirected table pairs and column pairs, in real lower-case names, against the declared FKs."""
    tables = {t.lower(): t for t in schema}
    columns = {(t, c.lower()): c for t, cols in schema.items() for c in cols}

    def resolve(t, c):
        """A (table, column) from the reply -> real lower-case names; column None when absent or unknown."""
        t = tables.get((t or "").strip().lower())
        if t is None:
            return None
        c = columns.get((t, (c or "").strip().lower()))
        rt, rc = back_to_run.get((t, c), (t, c)) if back_to_run else (t, c)
        if rc is None:
            return bl.back(mapping, db, rt).lower(), None
        real = bl.back(mapping, db, rt, rc)
        return real[0].lower(), real[1].lower()

    gold_t = {frozenset((a[0], b[0])) for a, b in fks}
    gold_c = {frozenset(p) for p in fks}
    pred_t, pred_c, invalid = set(), set(), 0
    for j in reply_joins:
        if not isinstance(j, dict):
            invalid += 1
            continue
        a, b = resolve(j.get("from_table"), j.get("from_column")), resolve(j.get("to_table"), j.get("to_column"))
        if a is None or b is None:
            invalid += 1
            continue
        pred_t.add(frozenset((a[0], b[0])))
        if a[1] and b[1]:
            pred_c.add(frozenset((a, b)))
    n = len(schema)
    return {"tables": prf(len(pred_t & gold_t), len(pred_t), len(gold_t)),
            "columns": prf(len(pred_c & gold_c), len(pred_c), len(gold_c)),
            "invalid": invalid, "table_pairs": n * (n + 1) // 2}


def total(scores, level):
    tp, pred, gold = (sum(s[level][k] for s in scores) for k in ("tp", "predicted", "gold"))
    return prf(tp, pred, gold)


# ------------------------------------------------------------------ blind adjudication
def blind_payload(rec):
    return {role: {"table": rec[role]["table"], "column": rec[role]["column"]} for role in ("source", "target")}


def tag(rec):
    return f"{rec['source']['table']}.{rec['source']['column']}->{rec['target']['table']}.{rec['target']['column']}"


def blind(llm, rec):
    reply, effort = llm.complete("blind_adjudicate", tag(rec), BLIND_ADJUDICATE,
                                 json.dumps(blind_payload(rec)), VERDICT_SCHEMA)
    reply = reply or {}
    verdict = reply.get("decision") if reply.get("decision") in VERDICTS else "abstain"
    return {"decision": verdict, "reason": reply.get("reason", "unparsable reply"), "reasoning_effort": effort}


def correct(verdict, is_gold):
    return verdict == ("accept" if is_gold else "reject")


def cross(rows):
    """Full x blind verdicts for one set of records."""
    c = Counter()
    for r in rows:
        f, b = correct(r["full"], r["gold"]), correct(r["blind"], r["gold"])
        c["both" if f and b else "full_only" if f else "blind_only" if b else "neither"] += 1
    full_ok = c["both"] + c["full_only"]
    return {"records": len(rows), "full_correct": full_ok, "blind_correct": c["both"] + c["blind_only"],
            "both": c["both"], "full_only": c["full_only"], "blind_only": c["blind_only"], "neither": c["neither"],
            "memory_reachable_share": share(c["both"], full_ok), "evidence_share": share(c["full_only"], full_ok),
            "disagreement": share(sum(r["full"] != r["blind"] for r in rows), len(rows)),
            "full_verdicts": dict(Counter(r["full"] for r in rows)),
            "blind_verdicts": dict(Counter(r["blind"] for r in rows))}


# ------------------------------------------------------------------ command line
def run_config(s3_dir, dbs, records):
    """The run's own model and reasoning effort, from its descriptions or else its verdicts."""
    models = {json.loads((s3_dir / f"{db}.json").read_text())["model"] for db in dbs if (s3_dir / f"{db}.json").exists()}
    models |= {r["llm"]["model"] for rs in records.values() for r in rs}
    efforts = {json.loads((s3_dir / f"{db}.json").read_text())["reasoning_effort"]
               for db in dbs if (s3_dir / f"{db}.json").exists()} or {DEFAULTS["reasoning_effort"]}
    if len(models) != 1 or len(efforts) != 1:
        sys.exit(f"the run mixes models {models} / reasoning efforts {efforts}; probe one setting at a time")
    return {**DEFAULTS, "model": models.pop(), "reasoning_effort": efforts.pop()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("repo", "dbfile", "stage3_dir"):
        ap.add_argument(name)
    ap.add_argument("--dataset", default="bird")
    ap.add_argument("--mapping", help='"position" or a mapping.json path')
    ap.add_argument("--databases", nargs="*", help="only these db_ids")
    ap.add_argument("--workers", type=int, default=DEFAULTS["workers"])
    ap.add_argument("--dry-run", action="store_true", help="no LLM calls")
    args = ap.parse_args()
    repo, s3_dir = Path(args.repo), Path(args.stage3_dir)
    con = duckdb.connect(str(Path(args.dbfile).expanduser()), read_only=True)
    dbs = [d["db_id"] for d in json.loads((repo / f"out/filtered/{args.dataset}/databases.json").read_text())
           if (s3_dir / "joins" / f"{d['db_id']}.json").exists() and (not args.databases or d["db_id"] in args.databases)]
    if not dbs:
        sys.exit(f"no finished databases in {s3_dir / 'joins'}")
    meta, label_schemas = bl.label_source(repo, args.dataset)
    mapping = bl.load_mapping(args.mapping, con, {db: label_schemas[db] for db in dbs})

    schemas, fks, records = {}, {}, {}
    for db in dbs:
        profile = json.loads((repo / "out" / "profiles" / args.dataset / f"{db}.json").read_text())
        schemas[db] = shown_schema(profile)
        fks[db] = bl.gold(con, meta, label_schemas[db])[1]
        gold_pairs = {frozenset(p) for p in fks[db]}
        joins = json.loads((s3_dir / "joins" / f"{db}.json").read_text())
        records[db] = [r for r in joins["candidates"] if r["decision"].startswith("llm_")]
        names = hidden_names(mapping, db)
        for r in records[db]:
            r["_gold"] = bl.norm_edge(mapping, db, r) in gold_pairs
            r["_leaked"] = leaks(r["llm"]["reason"], names)
    cfg = {**run_config(s3_dir, dbs, records), "workers": args.workers}

    # check: the band records and their classes are the ones grade_bird.py counted
    band = Counter(f"{r['decision']}|{'gold' if r['_gold'] else 'not_gold'}" for rs in records.values() for r in rs)
    grade_path = s3_dir / "grade.json"
    grade = json.loads(grade_path.read_text()) if grade_path.exists() else None
    if grade and set(grade["per_db"]) == set(dbs):
        check = "matches grade.json" if dict(band) == grade["band_verdicts"] else f"DIFFERS from grade.json {grade['band_verdicts']}"
    else:
        check = "not checked: grade.json missing or graded on other databases"
    n_band = sum(band.values())
    n_leak = sum(bool(r["_leaked"]) for rs in records.values() for r in rs)
    print(f"{s3_dir}: {len(dbs)} databases, {n_band} band records {dict(band)} ({check})")
    print(f"model {cfg['model']}, reasoning effort {cfg['reasoning_effort']}; "
          f"{n_leak} full-run reasons name a hidden column")

    if args.dry_run:
        db = next((d for d in dbs if records[d]), dbs[0])
        print("open recall payload:", json.dumps({"tables": schemas[db]})[:400])
        print("control payload:    ", json.dumps({"tables": opaque(schemas[db])[0]})[:400])
        if records[db]:
            print("blind payload:      ", json.dumps(blind_payload(records[db][0])))
        for rs in records.values():
            for r in rs:
                if r["_leaked"]:
                    print(f"  leak {tag(r)}: {r['_leaked']} | {r['llm']['reason'][:160]}")
        return

    out = s3_dir / "probe"
    out.mkdir(parents=True, exist_ok=True)
    llm = Voyager(cfg, out / "llm_calls.jsonl")

    def open_call(db, control):
        schema, back = opaque(schemas[db]) if control else (schemas[db], None)
        reply, _ = llm.complete("open_recall_control" if control else "open_recall", db, OPEN_RECALL,
                                json.dumps({"tables": schema}), OPEN_SCHEMA)
        got = (reply or {}).get("joins")
        got = got if isinstance(got, list) else []
        return {"reply": got, "ok": reply is not None,
                "score": score_open(got, schema, back, mapping, db, fks[db])}

    jobs = [(db, control) for db in dbs for control in (False, True)]
    flat = [(db, r) for db in dbs for r in records[db]]
    with ThreadPoolExecutor(cfg["workers"]) as pool:
        opens = pool.map(lambda j: open_call(*j), jobs)
        blinds = pool.map(lambda x: blind(llm, x[1]), flat)
        opens, blinds = list(opens), list(blinds)

    open_recall = {db: {} for db in dbs}
    for (db, control), o in zip(jobs, opens):
        open_recall[db]["control" if control else "probe"] = o
    rows = []
    for (db, r), b in zip(flat, blinds):
        names = hidden_names(mapping, db)
        rows.append({"db": db, "source": r["source"], "target": r["target"], "gold": r["_gold"],
                     "real": sorted(map(list, bl.norm_edge(mapping, db, r))),
                     "full": r["llm"]["decision"], "full_reason": r["llm"]["reason"], "full_leaked": r["_leaked"],
                     "blind": b["decision"], "blind_reason": b["reason"], "blind_leaked": leaks(b["reason"], names)})

    probes = [open_recall[db]["probe"]["score"] for db in dbs]
    controls = [open_recall[db]["control"]["score"] for db in dbs]
    gold_table_pairs = sum(s["tables"]["gold"] for s in probes)
    report = {
        "run": str(s3_dir), "dataset": args.dataset, "model": cfg["model"], "reasoning_effort": cfg["reasoning_effort"],
        "databases": dbs, "band_records": n_band, "band_check": check,
        "open_recall": {
            "probe": {"tables": total(probes, "tables"), "columns": total(probes, "columns"),
                      "invalid": sum(s["invalid"] for s in probes)},
            "control": {"tables": total(controls, "tables"), "columns": total(controls, "columns"),
                        "invalid": sum(s["invalid"] for s in controls)},
            "chance_table_precision": share(gold_table_pairs, sum(s["table_pairs"] for s in probes)),
            "failed_replies": sum(not o["ok"] for d in open_recall.values() for o in d.values())},
        "blind": {"gold": cross([r for r in rows if r["gold"]]), "not_gold": cross([r for r in rows if not r["gold"]]),
                  "pooled": cross(rows)},
        "leaks": {"full_reasons": sum(bool(r["full_leaked"]) for r in rows),
                  "blind_reasons": sum(bool(r["blind_leaked"]) for r in rows),
                  "names": dict(Counter(n for r in rows for n in r["full_leaked"] + r["blind_leaked"]))},
        "per_db": {db: {"open_recall": {k: v["score"] for k, v in open_recall[db].items()},
                        "band_records": len(records[db]),
                        "blind_gold": cross([r for r in rows if r["db"] == db and r["gold"]])}
                   for db in dbs}}

    (out / "open_recall.json").write_text(json.dumps(open_recall, indent=1, ensure_ascii=False))
    (out / "blind_verdicts.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    (out / "probe.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({k: v for k, v in report.items() if k != "per_db"}, indent=1))


if __name__ == "__main__":
    main()
