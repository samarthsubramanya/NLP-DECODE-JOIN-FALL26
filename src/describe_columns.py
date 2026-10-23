"""
Stage 3: a readable name and a one-line description for every column, from the Stage 1
profile and the Stage 2a keys, by a frozen LLM on ASU Research Computing's Voyager gateway.

One call per table. The model sees, per column: its name, declared and value-inferred
type, null rate, distinct count, top and sample values, numeric/date ranges, whether
Stage 2a picked it as the primary key, and value overlap with other tables' keys
(Stage 1 containment). Plus a few sample rows and the other table names. It answers in
a JSON schema built from the table's exact column names, so no column can be dropped,
and marks each one resolved / tentative / unresolved instead of guessing.

Same information boundary as Stages 1-2a: no declared keys, no *_metadata, no gold SQL,
no shipped descriptions (BIRD's database_description/ is an evaluation label).

Model: glm-5-3 by default, the largest general model on the gateway; any chat model id
from GET /v1/models works with --model (e.g. qwen36-27b, gemma4-31b-it). Temperature 0,
replies cached on disk by request hash, so reruns are free and identical.

Key: VOYAGER_API_KEY in the environment or in the repo's .env (Voyager portal > Keys).

Usage:
    python src/describe_columns.py --dataset bird
    python src/describe_columns.py --dataset spider --databases concert_singer --run try
    python src/describe_columns.py --dataset bird --names-only --run names_only   # RQ2 ablation

Input:  out/profiles/<dataset>/<db_id>.json, out/keys/<dataset>/<keys-run>/<db_id>.json
Output: out/descriptions/<dataset>/<run>/<db_id>.json, llm_calls.jsonl
        out/llm_cache/<hash>.json
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

from primary_keys import ROOT, load_profile, work_list

sys.path.insert(0, str(ROOT))
from poc.llm import api_key_from, parse_json   # noqa: E402  (key lookup and lenient JSON parse)

DEFAULTS = {
    "base_url": "https://openai.rc.asu.edu/v1",
    "model": "glm-5-3",
    "api_key_env": "VOYAGER_API_KEY",
    "temperature": 0,
    "reasoning_effort": "medium",   # thinking comes out of max_tokens, so keep that large
    "max_tokens": 32000,
    "timeout_s": 1800,              # the gateway queues up to 5 min and runs up to 60
    "workers": 4,                   # the gateway docs suggest 4-8 per model
    "retry_waits": [5, 10, 20, 40],
    "keys_run": "frozen",
    "link_min_containment": 0.95,   # same cut as Stage 2a R1
    "top_values": 5,
    "sample_values": 5,
    "sample_rows": 3,
    "max_value_len": 80,
}

STATUSES = ("resolved", "tentative", "unresolved")
ROLES = ("identifier", "foreign_key", "date", "numeric", "category", "code", "free_text", "boolean")

SYSTEM = """You document one table of a database that has no documentation.
For each column you get what the data shows: its name, declared and inferred type, null rate,
distinct values, top and sample values, value ranges, whether it was detected as the table's
primary key, and other tables' key columns that contain its values ("values_found_in", a
possible foreign key) or whose values it contains ("referenced_by"). These overlaps come from
values only: small integers often overlap by chance, so trust them only when the rest of the
evidence agrees. You also get sample rows
and the names of the other tables in the database. Names may be abbreviated, coded or opaque.

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


def links(profile, keys, cfg):
    """{(table, column): {"values_found_in": [...], "referenced_by": [...]}} toward predicted keys."""
    pks = {(t, k["primary_key"]) for t, k in keys["tables"].items() if k["primary_key"]}
    out = {}
    for pair in profile.get("containment", []):
        src, tgt = tuple(pair["source"]), tuple(pair["target"])
        if pair["containment"] < cfg["link_min_containment"] or tgt not in pks or src[0] == tgt[0]:
            continue
        out.setdefault(src, {}).setdefault("values_found_in", []).append(".".join(tgt))
        out.setdefault(tgt, {}).setdefault("referenced_by", []).append(".".join(src))
    return out


def payload(profile, keys, table, cfg, use_values):
    t, pk = profile["tables"][table], keys["tables"][table]["primary_key"]
    lk, n = links(profile, keys, cfg), cfg["max_value_len"]
    columns = {}
    for name, p in t["columns"].items():
        c = {"declared_type": p["declared_type"], "inferred_type": p["family"], "role_guess": p["role"],
             "null_rate": round(p["null_rate"], 3), "distinct": p["distinct"],
             "uniqueness": round(p["uniqueness"], 3), "is_primary_key": name == pk,
             **lk.get((table, name), {})}
        if use_values:
            c["top_values"] = [[clip(v, n), k] for v, k in p["top_values"][:cfg["top_values"]]]
            c["sample_values"] = [clip(v, n) for v in p["sample_values"][:cfg["sample_values"]]]
            if p.get("numeric"):
                c["numeric"] = {k: round(v, 3) if isinstance(v, float) else v for k, v in p["numeric"].items()
                                if k in ("min", "p25", "p50", "p75", "max", "mean")}
            if p.get("date_range"):
                c["date_range"] = p["date_range"]
        columns[name] = c
    rows = [{k: clip(v, n) for k, v in r.items()} for r in t["sample_rows"][:cfg["sample_rows"]]] if use_values else []
    return {"table": table, "rows": t["rows"], "other_tables": [o for o in profile["tables"] if o != table],
            "columns": columns, "sample_rows": rows}


def schema(columns):
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


class Voyager:
    """OpenAI-compatible chat call with a disk cache, retries and a call log."""

    def __init__(self, cfg, log_path):
        self.cfg, self.log_path = cfg, log_path
        self.cache_dir = ROOT / "out" / "llm_cache"
        self.key = None

    def complete(self, table, user, json_schema):
        body = {"model": self.cfg["model"], "temperature": self.cfg["temperature"],
                "max_tokens": self.cfg["max_tokens"], "reasoning_effort": self.cfg["reasoning_effort"],
                "response_format": {"type": "json_schema", "json_schema": json_schema},
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
        h = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        cached, start, usage = self.cache_dir / f"{h}.json", time.perf_counter(), None
        if cached.exists():
            text, source = json.loads(cached.read_text())["text"], "cache"
        else:
            text, usage = self._post(body)
            source = "api"
            if text:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps({"model": self.cfg["model"], "text": text}))
        parsed = parse_json(text)
        with self.log_path.open("a") as f:   # one short line per call; appends from threads don't interleave
            f.write(json.dumps({"table": table, "key": h, "source": source, "usage": usage,
                                "seconds": round(time.perf_counter() - start, 1), "ok": parsed is not None}) + "\n")
        return parsed

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


def describe_table(llm, profile, keys, table, cfg, use_values):
    cols = list(profile["tables"][table]["columns"])
    reply = llm.complete(table, json.dumps(payload(profile, keys, table, cfg, use_values), default=str),
                         schema(cols)) or {}
    got = reply.get("columns") or {}
    columns = {}
    for name in cols:
        c = got.get(name) or {}
        status = c.get("status") if c.get("status") in STATUSES else "unresolved"
        keep = status != "unresolved"
        columns[name] = {"role": c.get("role", ""), "name": c.get("name", "") if keep else "",
                         "description": c.get("description", "") if keep else "", "status": status}
    t = reply.get("table") or {}
    return {"table": {"name": t.get("name", ""), "purpose": t.get("purpose", "")}, "columns": columns,
            "ok": bool(reply)}


def describe_database(llm, profile, keys, cfg, use_values):
    with ThreadPoolExecutor(cfg["workers"]) as pool:
        futures = {t: pool.submit(describe_table, llm, profile, keys, t, cfg, use_values) for t in profile["tables"]}
        tables = {t: f.result() for t, f in futures.items()}
    return {"db_id": profile["db_id"], "model": cfg["model"], "reasoning_effort": cfg["reasoning_effort"],
            "use_values": use_values, "keys_run": cfg["keys_run"], "tables": tables}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--databases", nargs="*", help="only these db_ids")
    ap.add_argument("--run", default="default", help="output sub-folder, name it after what you changed")
    ap.add_argument("--keys-run", default=DEFAULTS["keys_run"], help="Stage 2a run to read primary keys from")
    ap.add_argument("--model", default=DEFAULTS["model"])
    ap.add_argument("--reasoning-effort", default=DEFAULTS["reasoning_effort"],
                    choices=["none", "low", "medium", "high"])
    ap.add_argument("--workers", type=int, default=DEFAULTS["workers"])
    ap.add_argument("--names-only", action="store_true", help="hide values and sample rows (RQ2 ablation)")
    args = ap.parse_args()
    cfg = {**DEFAULTS, "model": args.model, "reasoning_effort": args.reasoning_effort,
           "workers": args.workers, "keys_run": args.keys_run}

    out_dir = ROOT / "out" / "descriptions" / args.dataset / args.run
    out_dir.mkdir(parents=True, exist_ok=True)
    llm = Voyager(cfg, out_dir / "llm_calls.jsonl")
    keys_dir = ROOT / "out" / "keys" / args.dataset / args.keys_run
    for db in work_list(args):
        keys_path = keys_dir / f"{db['db_id']}.json"
        if not keys_path.exists():
            sys.exit(f"{keys_path.relative_to(ROOT)} is missing; run src/primary_keys.py --run {args.keys_run} first")
        out = describe_database(llm, load_profile(args.dataset, db), json.loads(keys_path.read_text()),
                                cfg, not args.names_only)
        counts = {s: sum(c["status"] == s for t in out["tables"].values() for c in t["columns"].values())
                  for s in STATUSES}
        failed = [t for t, v in out["tables"].items() if not v["ok"]]
        print(f"  describe {db['db_id']}: {counts}" + (f"  no reply for {failed}" if failed else ""))
        (out_dir / f"{db['db_id']}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
