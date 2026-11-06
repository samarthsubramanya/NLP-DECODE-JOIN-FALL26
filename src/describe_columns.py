"""
Stage 3: the LLM step. First it judges the ambiguous Stage 2b joins, then it writes a readable
name and a one-line description for every column, from the final join graph. A frozen LLM on
ASU Research Computing's Voyager gateway; nothing is trained.

Step 1, adjudication. Stage 2b scores every candidate join and labels the weakest accepted
ones, score in [accept, accept + delta), as "band". Almost all band records are there because
their source column has several surviving targets and the fan-out factor psi pulled them all
down, so by default (--adjudication grouped) the band records of one source column go to the
LLM together, one call per source: the source column's profile and sample rows once, then each
competing target with its profile, sample rows, other columns, Stage 2a score and the Stage 2b
score with its parts, penalties and fan-out, plus the targets of that source statistics already
accepted outright. The LLM accepts, rejects or abstains on each candidate while seeing its
rivals; it may accept several or none, and cannot propose a join. --adjudication single sends
one call per band record instead (the earlier behaviour). An unreadable reply counts as
abstain. Verdicts are written
on the records as llm_accept / llm_reject / llm_abstain with the model's reason, and
    graphs.llm = accepted + llm_accept
is built with Stage 2b's own greedy assembly. Rejected and abstained records stay in the file
with their reason but are not in graphs.llm, so they are never exported.

Step 2, descriptions (one call per table). The model sees, per column: its name, declared
and value-inferred type, null rate, distinct count, top and sample values, numeric/date
ranges, whether Stage 2a picked it as the primary key, and its joins in graphs.llm. When a
column references a small lookup table (<= lookup_max_rows rows) through such a join, a few
key -> label pairs from that table's sample rows are added. Plus sample rows and the other
table names. It answers in a JSON schema built from the table's exact column names, so no
column can be dropped, and marks each one resolved / tentative / unresolved instead of
guessing. Descriptions never change the graph.

Same information boundary as Stages 1-2b: only out/profiles, out/keys and out/joins. No
declared keys, no *_metadata, no gold SQL, no shipped descriptions (BIRD's
database_description/ is an evaluation label).

Model: glm-5-3 by default, the largest general model on the gateway; any chat model id
from GET /v1/models works with --model (e.g. qwen36-27b, gemma4-31b-it). Temperature 0,
replies cached on disk by request hash, so reruns are free and identical.

Key: VOYAGER_API_KEY in the environment or in the repo's .env (Voyager portal > Keys).

Usage:
    python src/describe_columns.py --dataset bird
    python src/describe_columns.py --dataset bird --adjudication single --run single   # one call per band record
    python src/describe_columns.py --dataset bird --no-describe --run joins_only       # step 1 only
    python src/describe_columns.py --dataset bird --databases financial --run try
    python src/describe_columns.py --dataset bird --names-only --run names_only        # RQ2 ablation
    python src/describe_columns.py --dataset bird --joins-run no_names --run no_names  # obfuscated simulation
    python src/describe_columns.py --dataset bird_stripped --reasoning-effort low --run ""  # stripped file
--run "" writes straight into out/descriptions/<dataset>/ (one run per setting, no sub-folder).

Input:  out/profiles/<dataset>/<db_id>.json
        out/joins/<dataset>/<joins-run>/<db_id>.json   (src/foreign_keys.py)
        out/keys/<dataset>/<keys_run of that joins file>/<db_id>.json
Output: out/descriptions/<dataset>/<run>/<db_id>.json         descriptions
        out/descriptions/<dataset>/<run>/joins/<db_id>.json   the Stage 2b file + verdicts + graphs.llm
        out/descriptions/<dataset>/<run>/llm_calls.jsonl, out/llm_cache/<hash>.json
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from foreign_keys import assemble
from primary_keys import ROOT, load_profile, work_list

sys.path.insert(0, str(ROOT))
from poc.llm import api_key_from, parse_json   # noqa: E402  (key lookup and lenient JSON parse)

DEFAULTS = {
    "base_url": "https://openai.rc.asu.edu/v1",
    "model": "glm-5-3",
    "api_key_env": "VOYAGER_API_KEY",
    "temperature": 0,
    "reasoning_effort": "medium",   # thinking comes out of max_tokens, so keep that large
    # a reply with no content means the thinking used all of max_tokens (seen on glm-5-3, ~1% of
    # calls, 11-16 min each); it is asked once more with this effort, and the effort used is recorded
    "fallback_effort": "none",
    "max_tokens": 32000,
    "timeout_s": 1800,              # the gateway queues up to 5 min and runs up to 60
    "workers": 4,                   # the gateway docs suggest 4-8 per model
    "retry_waits": [5, 10, 20, 40],
    "joins_run": "default",
    "repeat": 0,                    # >0: ask again instead of replaying the cache (variance runs)
    "adjudication": "grouped",      # grouped: one call per source column; single: one per band record
    "top_values": 5,
    "sample_values": 5,
    "sample_rows": 3,
    "max_value_len": 80,
    "lookup_max_rows": 100,         # a join target this small is treated as a code -> label lookup
    "lookup_label_roles": ["text", "code", "free_text"],
}

VERDICTS = ("accept", "reject", "abstain")
# added to a candidate only when Stage 2b's repair proposed it, so every other prompt stays as it was
PARTIAL_NOTE = ("Proposed because this table has no other join. The tables may be samples or truncated extracts of "
                "larger tables, so low containment alone is not evidence against the join: judge whether the two "
                "columns hold the same identifiers. Shared common values, such as first names, cities or years, are not a join.")
STATUSES = ("resolved", "tentative", "unresolved")
ROLES = ("identifier", "foreign_key", "date", "numeric", "category", "code", "free_text", "boolean")

ADJUDICATE = """You review one candidate join between two columns of a database you have never seen.
Statistics already scored it just above the acceptance cutoff, so the call is uncertain.

Decide whether the source column really references the target column, i.e. whether each
source value names the same real-world entity as the matching target value. You get both
columns' profiles, sample rows of both tables, the other columns of each table, the target's
primary-key score and the join score with its parts.

Overlapping values alone are not enough: small integers, years and codes often overlap by
chance. Matching names alone are not enough either. You may only judge this candidate; do
not suggest other joins.

When the source column is its own table's primary key ("source_is_primary_key"), a real join is
usually a subtype or extension link: the source table holds some of the target table's entities
(an employee is a person, a savings account is an account) and reuses the target's key, so
containment is full, coverage is low and both keys come from one shared ID pool. A shared ID pool
is then expected, not a sign of coincidence: ask whether the source entity can be a kind of, or a
one-to-one extension of, the target entity, and reject only when it cannot.

accept   the evidence shows a real reference
reject   the evidence shows the overlap is coincidental or the columns mean different things
abstain  the evidence is too thin to tell

Answer with JSON only: {"decision": "accept" | "reject" | "abstain", "reason": "<one sentence>"}"""

ADJUDICATE_GROUP = """You review the candidate joins of one source column in a database you have never seen.
Statistics found several target columns that this source column could reference and scored each
just above the acceptance cutoff; they were pulled down because they compete, so the call is uncertain.

For each candidate, decide whether the source column really references that target column, i.e.
whether each source value names the same real-world entity as the matching target value. You get
the source column's profile and sample rows once, then every candidate target with its profile,
sample rows, other columns, primary-key score and join score with its parts. "already_accepted"
lists targets of this source that statistics accepted outright.

Compare the candidates with each other. Usually one target is the real reference and the others
overlap by chance, but more than one can be right (the same entity kept in two tables), and none
may be. Overlapping values alone are not enough: small integers, years and codes often overlap by
chance. Matching names alone are not enough either. Judge only these candidates; do not suggest
other joins.

When the source column is its own table's primary key ("source.is_primary_key"), a real join is
usually a subtype or extension link: the source table holds some of the target table's entities
(an employee is a person, a savings account is an account) and reuses the target's key, so
containment is full, coverage is low and both keys come from one shared ID pool. A shared ID pool
is then expected, not a sign of coincidence: ask whether the source entity can be a kind of, or a
one-to-one extension of, the target entity, and reject only when it cannot.

accept   the evidence shows a real reference
reject   the evidence shows the overlap is coincidental or the columns mean different things
abstain  the evidence is too thin to tell

Answer with JSON only, in the given schema: one decision and a one-sentence reason per candidate id."""

DESCRIBE = """You document one table of a database that has no documentation.
For each column you get what the data shows: its name, declared and inferred type, null rate,
distinct values, top and sample values, value ranges, whether it was detected as the table's
primary key, and its accepted joins to other tables ("references" or "referenced_by"). When a
column references a small lookup table, a few of that table's key -> label pairs are included.
You also get sample rows and the names of the other tables in the database. Names may be
abbreviated, coded or opaque.

Infer what the table and each column mean. Read the values, not just the name: units, code
meanings, what a row represents. Do not guess. If the evidence does not support a meaning,
say so and mark the column unresolved.

status:
  resolved   the evidence clearly supports the meaning
  tentative  plausible, but another reading fits too; say which in the description
  unresolved not enough evidence; leave name and description empty

Answer with JSON only, in the given schema, one entry per column under its exact key."""


def clip(v, n):
    return v[:n] + "…" if isinstance(v, str) and len(v) > n else v


def rows_of(table_profile, cfg, use_values=True):
    n = cfg["max_value_len"]
    return [{k: clip(v, n) for k, v in r.items()} for r in table_profile["sample_rows"][:cfg["sample_rows"]]] \
        if use_values else []


def column_evidence(p, cfg, use_values=True):
    n = cfg["max_value_len"]
    c = {"declared_type": p["declared_type"], "inferred_type": p["family"], "role_guess": p["role"],
         "null_rate": round(p["null_rate"], 3), "distinct": p["distinct"], "uniqueness": round(p["uniqueness"], 3)}
    if use_values:
        c["top_values"] = [[clip(v, n), k] for v, k in p["top_values"][:cfg["top_values"]]]
        c["sample_values"] = [clip(v, n) for v in p["sample_values"][:cfg["sample_values"]]]
        if p.get("numeric"):
            c["numeric"] = {k: round(v, 3) if isinstance(v, float) else v for k, v in p["numeric"].items()
                            if k in ("min", "p25", "p50", "p75", "max", "mean")}
        if p.get("date_range"):
            c["date_range"] = p["date_range"]
    return c


# ------------------------------------------------------------------ step 1: adjudicate band joins
def adjudication_payload(rec, profile, joins, cfg):
    side = {}
    for role in ("source", "target"):
        t, c = rec[role]["table"], rec[role]["column"]
        tp = profile["tables"][t]
        side[role] = {"table": t, "column": c, "table_rows": tp["rows"],
                      "profile": column_evidence(tp["columns"][c], cfg),
                      "other_columns": [x for x in tp["columns"] if x != c], "sample_rows": rows_of(tp, cfg)}
    return {**side, **({"note": PARTIAL_NOTE} if rec.get("partial_overlap") else {}),
            "source_is_primary_key": rec["source_is_pk"],
            "target_is_primary_key": rec["target_is_pk"], "target_pk_score": rec["target_pk_score"],
            "containment": round(rec["containment"], 3), "coverage": round(rec["coverage"], 3),
            "cardinality": rec["cardinality"], "join_score": round(rec["score"], 1),
            "join_score_parts": rec["parts"], "weights": joins["weights"], "penalties": rec["penalties"],
            "fan_out": rec["fan_out"], "acceptance_cutoff": joins["config"]["accept"],
            "band": [joins["config"]["accept"], joins["config"]["accept"] + joins["config"]["delta"]]}


VERDICT_SCHEMA = {"name": "join_verdict", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["decision", "reason"],
    "properties": {"decision": {"type": "string", "enum": list(VERDICTS)}, "reason": {"type": "string"}}}}


def judge(llm, rec, profile, joins, cfg):
    tag = f"{rec['source']['table']}.{rec['source']['column']}->{rec['target']['table']}.{rec['target']['column']}"
    reply, effort = llm.complete("adjudicate", tag, ADJUDICATE,
                                 json.dumps(adjudication_payload(rec, profile, joins, cfg), default=str), VERDICT_SCHEMA)
    reply = reply or {}
    verdict = reply.get("decision") if reply.get("decision") in VERDICTS else "abstain"
    return {"decision": verdict, "reason": reply.get("reason", "unparsable reply"), "reasoning_effort": effort}


def group_payload(recs, profile, joins, cfg):
    s = recs[0]["source"]
    tp = profile["tables"][s["table"]]
    accepted = [f"{r['target']['table']}.{r['target']['column']}" for r in joins["candidates"]
                if r["decision"] == "accepted" and r["source"] == s]
    cands = {}
    for i, rec in enumerate(recs, 1):
        t, c = rec["target"]["table"], rec["target"]["column"]
        tt = profile["tables"][t]
        cands[f"c{i}"] = {"table": t, "column": c, "table_rows": tt["rows"], "profile": column_evidence(tt["columns"][c], cfg),
                          "other_columns": [x for x in tt["columns"] if x != c], "sample_rows": rows_of(tt, cfg),
                          "target_is_primary_key": rec["target_is_pk"], "target_pk_score": rec["target_pk_score"],
                          "containment": round(rec["containment"], 3), "coverage": round(rec["coverage"], 3),
                          "cardinality": rec["cardinality"], "join_score": round(rec["score"], 1),
                          "join_score_parts": rec["parts"], "penalties": rec["penalties"], "fan_out": rec["fan_out"],
                          **({"note": PARTIAL_NOTE} if rec.get("partial_overlap") else {})}
    return {"source": {"table": s["table"], "column": s["column"], "table_rows": tp["rows"],
                       "is_primary_key": recs[0]["source_is_pk"],
                       "profile": column_evidence(tp["columns"][s["column"]], cfg),
                       "other_columns": [x for x in tp["columns"] if x != s["column"]], "sample_rows": rows_of(tp, cfg)},
            "already_accepted": accepted, "candidates": cands, "weights": joins["weights"],
            "acceptance_cutoff": joins["config"]["accept"],
            "band": [joins["config"]["accept"], joins["config"]["accept"] + joins["config"]["delta"]]}


def group_schema(ids):
    one = VERDICT_SCHEMA["schema"]
    return {"name": "join_verdicts", "strict": True, "schema": {
        "type": "object", "additionalProperties": False, "required": ["candidates"],
        "properties": {"candidates": {"type": "object", "additionalProperties": False, "required": list(ids),
                                      "properties": {i: one for i in ids}}}}}


def judge_group(llm, recs, profile, joins, cfg):
    s = recs[0]["source"]
    payload = group_payload(recs, profile, joins, cfg)
    reply, effort = llm.complete("adjudicate", f"{s['table']}.{s['column']} ({len(recs)} targets)", ADJUDICATE_GROUP,
                                 json.dumps(payload, default=str), group_schema(payload["candidates"]))
    got = (reply or {}).get("candidates") or {}
    out = []
    for i in payload["candidates"]:
        v = got.get(i) or {}
        out.append({"decision": v.get("decision") if v.get("decision") in VERDICTS else "abstain",
                    "reason": v.get("reason", "unparsable reply"), "reasoning_effort": effort, "group_size": len(recs)})
    return out


def adjudicate(llm, profile, keys, joins, cfg):
    """Verdicts on the band records (in place), then graphs.llm. Returns the verdict counts."""
    # records judged by an earlier Stage 3 run are judged again, so a model or prompt change takes effect
    band = [r for r in joins["candidates"] if r["decision"] == "band" or r["decision"].startswith("llm_")]
    for r in band:   # a key can be a source only when Stage 2b's G8 spared it (a sparse key)
        r["source_is_pk"] = keys["tables"][r["source"]["table"]]["primary_key"] == r["source"]["column"]
    with ThreadPoolExecutor(cfg["workers"]) as pool:
        if cfg["adjudication"] == "single":
            verdicts = list(pool.map(lambda r: judge(llm, r, profile, joins, cfg), band))
        else:
            groups = {}
            for r in band:
                groups.setdefault((r["source"]["table"], r["source"]["column"]), []).append(r)
            band = [r for g in groups.values() for r in g]
            verdicts = [v for vs in pool.map(lambda g: judge_group(llm, g, profile, joins, cfg), groups.values())
                        for v in vs]
    for rec, v in zip(band, verdicts):
        rec["llm"] = {**v, "model": cfg["model"], "adjudication": cfg["adjudication"]}
        rec["decision"] = f"llm_{v['decision']}"
    joins["graphs"]["llm"] = assemble(joins["candidates"], {"accepted", "llm_accept"}, joins["config"])
    return {v: sum(r["decision"] == f"llm_{v}" for r in band) for v in VERDICTS}


# ------------------------------------------------------------------ step 2: describe columns
def lookup_pairs(profile, target, cfg):
    """Up to sample_rows (key, label) pairs when the join target is a small table with a label column."""
    tp = profile["tables"][target["table"]]
    if tp["rows"] > cfg["lookup_max_rows"]:
        return None
    labels = sorted((c for c, p in tp["columns"].items()
                     if c != target["column"] and p["role"] in cfg["lookup_label_roles"]),
                    key=lambda c: cfg["lookup_label_roles"].index(tp["columns"][c]["role"]))
    if not labels:
        return None
    n = cfg["max_value_len"]
    return {"label_column": labels[0], "pairs": [[r[target["column"]], clip(r[labels[0]], n)] for r in tp["sample_rows"]
                                                 if r.get(target["column"]) is not None]}


def column_joins(profile, edges, cfg, use_values=True):
    """{(table, column): [join, ...]} from the final graph, both directions. Lookup labels are values."""
    out = {}
    for e in edges:
        via = "llm" if e["decision"] == "llm_accept" else "statistics"
        s, t = e["source"], e["target"]
        ref = {"to": f"{t['table']}.{t['column']}", "direction": "references", "cardinality": e["cardinality"],
               "accepted_by": via}
        pairs = use_values and lookup_pairs(profile, t, cfg)
        if pairs:
            ref["lookup"] = pairs
        out.setdefault((s["table"], s["column"]), []).append(ref)
        out.setdefault((t["table"], t["column"]), []).append(
            {"to": f"{s['table']}.{s['column']}", "direction": "referenced_by", "cardinality": e["cardinality"],
             "accepted_by": via})
    return out


def description_payload(profile, keys, links, table, cfg, use_values):
    t, pk = profile["tables"][table], keys["tables"][table]["primary_key"]
    columns = {name: {**column_evidence(p, cfg, use_values), "is_primary_key": name == pk,
                      "joins": links.get((table, name), [])}
               for name, p in t["columns"].items()}
    return {"table": table, "rows": t["rows"], "other_tables": [o for o in profile["tables"] if o != table],
            "columns": columns, "sample_rows": rows_of(t, cfg, use_values)}


def description_schema(columns):
    col = {"type": "object", "additionalProperties": False,
           "required": ["role", "name", "description", "status"],
           "properties": {"role": {"type": "string", "enum": list(ROLES)}, "name": {"type": "string"},
                          "description": {"type": "string"}, "status": {"type": "string", "enum": list(STATUSES)}}}
    return {"name": "table_description", "strict": True, "schema": {
        "type": "object", "additionalProperties": False, "required": ["table", "columns"],
        "properties": {
            "table": {"type": "object", "additionalProperties": False, "required": ["name", "purpose"],
                      "properties": {"name": {"type": "string"}, "purpose": {"type": "string"}}},
            "columns": {"type": "object", "additionalProperties": False, "required": list(columns),
                        "properties": {c: col for c in columns}}}}}


def describe_table(llm, profile, keys, links, table, cfg, use_values):
    cols = list(profile["tables"][table]["columns"])
    payload = description_payload(profile, keys, links, table, cfg, use_values)
    reply, effort = llm.complete("describe", table, DESCRIBE, json.dumps(payload, default=str), description_schema(cols))
    reply = reply or {}
    got = reply.get("columns") or {}
    columns = {}
    for name in cols:
        c = got.get(name) or {}
        status = c.get("status") if c.get("status") in STATUSES else "unresolved"
        if not (c.get("description") or "").strip():   # a status without a description says nothing
            status = "unresolved"
        keep = status != "unresolved"
        columns[name] = {"role": c.get("role", ""), "name": c.get("name", "") if keep else "",
                         "description": c.get("description", "") if keep else "", "status": status}
    t = reply.get("table") or {}
    return {"table": {"name": t.get("name", ""), "purpose": t.get("purpose", "")}, "columns": columns,
            "ok": bool(reply), "reasoning_effort": effort}


def describe(llm, profile, keys, joins, cfg, use_values):
    links = column_joins(profile, joins["graphs"]["llm"], cfg, use_values)
    with ThreadPoolExecutor(cfg["workers"]) as pool:
        futures = {t: pool.submit(describe_table, llm, profile, keys, links, t, cfg, use_values)
                   for t in profile["tables"]}
        return {t: f.result() for t, f in futures.items()}


# ------------------------------------------------------------------ LLM
class Voyager:
    """OpenAI-compatible chat call with a disk cache, retries and a call log. complete() -> (parsed, effort used)."""

    def __init__(self, cfg, log_path):
        self.cfg, self.log_path = cfg, log_path
        self.cache_dir = ROOT / "out" / "llm_cache"
        self.key = None

    def complete(self, step, tag, system, user, json_schema):
        body = {"model": self.cfg["model"], "temperature": self.cfg["temperature"],
                "max_tokens": self.cfg["max_tokens"], "reasoning_effort": self.cfg["reasoning_effort"],
                "response_format": {"type": "json_schema", "json_schema": json_schema},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        # repeat > 0 gives the same request a fresh cache slot: a new sample of the model, itself cached
        h = hashlib.sha256((json.dumps(body, sort_keys=True) + (f"#{self.cfg['repeat']}" if self.cfg["repeat"] else "")).encode()).hexdigest()
        cached, start, usage = self.cache_dir / f"{h}.json", time.perf_counter(), None
        effort = body["reasoning_effort"]
        if cached.exists():
            hit = json.loads(cached.read_text())
            text, source, effort = hit["text"], "cache", hit.get("reasoning_effort", effort)
        else:
            text, usage = self._post(body)
            source = "api"
            if not text and self.cfg["fallback_effort"] != effort:
                effort = self.cfg["fallback_effort"]
                text, usage = self._post({**body, "reasoning_effort": effort})
                source = "api_fallback"
            if text:   # stored under the first request's hash, so a rerun replays the same answer
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps({"model": self.cfg["model"], "reasoning_effort": effort, "text": text}))
        parsed = parse_json(text)
        with self.log_path.open("a") as f:   # one short line per call; appends from threads don't interleave
            f.write(json.dumps({"step": step, "item": tag, "key": h, "source": source, "reasoning_effort": effort,
                                "usage": usage, "seconds": round(time.perf_counter() - start, 1),
                                "ok": parsed is not None}) + "\n")
        return parsed, effort

    def _post(self, body):
        self.key = self.key or api_key_from(self.cfg["api_key_env"])
        req = urllib.request.Request(
            self.cfg["base_url"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
        waits = list(self.cfg["retry_waits"])
        while True:
            try:
                with urllib.request.urlopen(req, timeout=self.cfg["timeout_s"]) as resp:
                    data = json.loads(resp.read())
                return data["choices"][0]["message"].get("content"), data.get("usage")
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                if e.code not in (429, 502, 503, 504) or not waits:
                    raise RuntimeError(f"Voyager {e.code}: {detail}") from None
                time.sleep(60 if e.code == 429 else waits.pop(0))
            except (urllib.error.URLError, TimeoutError):
                if not waits:
                    raise
                time.sleep(waits.pop(0))


# ------------------------------------------------------------------ command line
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--databases", nargs="*", help="only these db_ids")
    ap.add_argument("--run", default="default",
                    help='output sub-folder, name it after what you changed; "" = out/descriptions/<dataset>/ itself')
    ap.add_argument("--joins-run", default=DEFAULTS["joins_run"],
                    help="Stage 2b run to read; its keys_run says which Stage 2a keys are used")
    ap.add_argument("--model", default=DEFAULTS["model"])
    ap.add_argument("--reasoning-effort", default=DEFAULTS["reasoning_effort"],
                    choices=["none", "low", "medium", "high"])
    ap.add_argument("--workers", type=int, default=DEFAULTS["workers"])
    ap.add_argument("--adjudication", default=DEFAULTS["adjudication"], choices=["grouped", "single"],
                    help="grouped: one call per source column with its competing band targets; single: one per record")
    ap.add_argument("--no-describe", action="store_true", help="step 1 only: judge the band and write graphs.llm")
    ap.add_argument("--repeat", type=int, default=0,
                    help="N > 0 bypasses the cached replies and asks again (cached as repeat N), to measure run-to-run variance")
    ap.add_argument("--names-only", action="store_true",
                    help="descriptions without values, sample rows or lookup labels (RQ2 ablation); "
                         "adjudication is unchanged")
    args = ap.parse_args()
    cfg = {**DEFAULTS, "model": args.model, "reasoning_effort": args.reasoning_effort,
           "workers": args.workers, "joins_run": args.joins_run, "adjudication": args.adjudication,
           "repeat": args.repeat}

    out_dir = ROOT / "out" / "descriptions" / args.dataset / args.run
    (out_dir / "joins").mkdir(parents=True, exist_ok=True)
    llm = Voyager(cfg, out_dir / "llm_calls.jsonl")
    joins_dir = ROOT / "out" / "joins" / args.dataset / args.joins_run
    for db in work_list(args):
        joins_path = joins_dir / f"{db['db_id']}.json"
        if not joins_path.exists():
            sys.exit(f"{joins_path.relative_to(ROOT)} is missing; run src/foreign_keys.py --run {args.joins_run} first")
        joins = json.loads(joins_path.read_text())
        keys_path = ROOT / "out" / "keys" / args.dataset / joins["keys_run"] / f"{db['db_id']}.json"
        if not keys_path.exists():
            sys.exit(f"{keys_path.relative_to(ROOT)} is missing; run src/primary_keys.py --run {joins['keys_run']} first")
        profile, keys = load_profile(args.dataset, db), json.loads(keys_path.read_text())

        verdicts = adjudicate(llm, profile, keys, joins, cfg)
        (out_dir / "joins" / f"{db['db_id']}.json").write_text(json.dumps(joins, indent=1, sort_keys=True))
        if args.no_describe:
            print(f"  {db['db_id']}: band {sum(verdicts.values())} -> {verdicts}, "
                  f"{len(joins['graphs']['direct'])} direct + LLM = {len(joins['graphs']['llm'])} edges")
            continue
        tables = describe(llm, profile, keys, joins, cfg, not args.names_only)
        out = {"db_id": db["db_id"], "model": cfg["model"], "reasoning_effort": cfg["reasoning_effort"],
               "use_values": not args.names_only, "joins_run": args.joins_run, "keys_run": joins["keys_run"],
               "graph": "llm", "edges": len(joins["graphs"]["llm"]), "tables": tables}
        (out_dir / f"{db['db_id']}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))

        counts = {s: sum(c["status"] == s for t in tables.values() for c in t["columns"].values()) for s in STATUSES}
        failed = [t for t, v in tables.items() if not v["ok"]]
        print(f"  {db['db_id']}: band {sum(verdicts.values())} -> {verdicts}, "
              f"{len(joins['graphs']['direct'])} direct + LLM = {len(joins['graphs']['llm'])} edges; "
              f"describe {counts}" + (f"  no reply for {failed}" if failed else ""))


if __name__ == "__main__":
    main()
