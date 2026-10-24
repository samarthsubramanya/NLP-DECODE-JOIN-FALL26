"""Grade a BIRD dev run of Stages 2a, 2b and 3 against BIRD's declared keys and reference descriptions.

These are evaluation labels, read only here: table_xinfo / foreign_key_list in bird_metadata.table_info,
and database_description/*.csv in bird_metadata.files, both from the BIRD DuckDB file.

    primary keys  single-column declared keys; P = correct / predicted (R5 name-only guesses excluded)
    joins         declared FKs as undirected column pairs ("references t" = t's key); P/R/F1 per graph
                  (direct, stats, llm), micro-averaged over databases; plus the LLM verdict x gold table
    descriptions  "name description" vs the reference "column_name column_description value_description":
                  cosine (qwen3-embedding-8b on Voyager), token F1, cosine against the next column's
                  reference (chance level) and whether the own reference ranks first in its table

Usage (from the repo root):
    python evals/grade_bird.py . <bird.duckdb> <label> <keys_dir> <joins_dir> <stage3_dir> [--dataset D] [--mapping M]
    python evals/grade_bird.py . ~/Downloads/decode_join_bird_dev.duckdb names \\
        out/keys/bird/frozen out/joins/bird/default out/descriptions/bird/names
    python evals/grade_bird.py . ~/Downloads/decode_join_bird_dev.duckdb obfuscated_low \\
        out/keys/bird_obfuscated/frozen_no_names out/joins/bird_obfuscated/frozen_no_names \\
        out/descriptions/bird_obfuscated/obfuscated_low --dataset bird_obfuscated --mapping position
<bird.duckdb> is always the documented BIRD file: it holds the labels.
--dataset   which out/filtered/<dataset>/databases.json lists the databases (default bird).
--mapping   how run names map back to real ones: "position" for bird_dev_obfuscated.duckdb
            (col_NNN = the NNN-th column of the documented table; written to <stage3_dir>/mapping.json),
            or a mapping.json path; omitted = names are real.
ONLY_DONE=1 grades only the databases the Stage 3 run has finished. Needs VOYAGER_API_KEY in .env.

Output: <stage3_dir>/grade.json, <stage3_dir>/grade_columns.jsonl (one row per column), and a summary.
"""
import argparse
import csv
import io
import json
import math
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import duckdb

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
for name in ("repo", "dbfile", "label", "keys_dir", "joins_dir", "stage3_dir"):
    ap.add_argument(name)
ap.add_argument("--dataset", default="bird")
ap.add_argument("--mapping", help='"position" or a mapping.json path')
args = ap.parse_args()
repo, dbfile, keys_dir, joins_dir, s3_dir = (Path(x) for x in (args.repo, args.dbfile, args.keys_dir, args.joins_dir,
                                                                args.stage3_dir))
label = args.label
con = duckdb.connect(str(dbfile), read_only=True)
dbs = [d["db_id"] for d in json.loads((repo / f"out/filtered/{args.dataset}/databases.json").read_text())]
if os.environ.get("ONLY_DONE"):   # grade only databases this Stage 3 run has finished
    dbs = [d for d in dbs if (s3_dir / f"{d}.json").exists()]


def position_mapping():
    """{db: {"tables": {t: t}, "columns": {t: {real: col_NNN}}}} from the documented file's column order."""
    out = {}
    for db in dbs:
        cols = defaultdict(dict)
        for t, c, pos in con.execute("SELECT table_name, column_name, ordinal_position FROM information_schema.columns "
                                     "WHERE table_schema = ? ORDER BY table_name, ordinal_position", [f"bird_{db}"]).fetchall():
            cols[t][c] = f"col_{pos:03d}"
        out[db] = {"tables": {t: t for t in cols}, "columns": dict(cols)}
    return out


if args.mapping == "position":
    mapping = position_mapping()
    s3_dir.mkdir(parents=True, exist_ok=True)
    (s3_dir / "mapping.json").write_text(json.dumps(mapping, indent=1))
else:
    mapping = json.loads(Path(args.mapping).read_text()) if args.mapping else None


def back(db, t, c=None):
    """obfuscated -> real name (identity without a mapping)."""
    if not mapping:
        return (t, c) if c is not None else t
    m = mapping[db]
    rt = {v: k for k, v in m["tables"].items()}[t]
    return (rt, {v: k for k, v in m["columns"][rt].items()}[c]) if c is not None else rt


# ------------------------------------------------------------------ gold
def gold(db):
    pks, fks = {}, set()
    rows = con.execute("SELECT t.table_name, t.metadata_json FROM bird_metadata.table_info t "
                       "JOIN bird_metadata.files f USING (source_path) WHERE f.duckdb_schema = ?", [f"bird_{db}"]).fetchall()
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


def ref_descriptions(db):
    out = {}
    for path, b in con.execute("SELECT source_path, original_bytes FROM bird_metadata.files WHERE source_path LIKE ?",
                               [f"dev_databases/{db}/database_description/%.csv"]).fetchall():
        raw = bytes(b)
        text = next(raw.decode(e) for e in ("utf-8-sig", "cp1252", "latin-1") if _decodes(raw, e))
        t = Path(path).stem.lower()
        for r in csv.DictReader(io.StringIO(text)):
            r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
            name = r.get("original_column_name", "")
            ref = " ".join(x for x in (r.get("column_name", ""), r.get("column_description", ""),
                                       r.get("value_description", "")) if x)
            if name:
                out[(t, name.lower())] = ref
    return out


def _decodes(b, e):
    try:
        b.decode(e)
        return True
    except UnicodeDecodeError:
        return False


# ------------------------------------------------------------------ metrics
def prf(tp, pred, gold_n):
    p = tp / pred if pred else 0.0
    r = tp / gold_n if gold_n else 0.0
    return {"tp": tp, "predicted": pred, "gold": gold_n, "precision": round(p, 4), "recall": round(r, 4),
            "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0}


def toks(s):
    return re.findall(r"[a-z0-9]+", s.lower())


def token_f1(a, b):
    ca, cb = Counter(toks(a)), Counter(toks(b))
    common = sum((ca & cb).values())
    if not common:
        return 0.0
    p, r = common / sum(ca.values()), common / sum(cb.values())
    return 2 * p * r / (p + r)


KEY = next(l.split("=", 1)[1].strip() for l in (repo / ".env").read_text().splitlines() if l.startswith("VOYAGER_API_KEY="))


def _embed_batch(batch):
    body = {"model": "qwen3-embedding-8b", "input": batch}
    req = urllib.request.Request("https://openai.rc.asu.edu/v1/embeddings", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return [d["embedding"] for d in sorted(json.loads(r.read())["data"], key=lambda d: d["index"])]


def embed(texts):
    """One vector per text. The gateway sometimes returns a batch with vectors swapped or copied between
    texts (seen 2026-10-24), so each batch is fetched twice and kept only when both copies agree."""
    uniq = sorted({t or " " for t in texts})
    vec = {}
    for i in range(0, len(uniq), 32):
        batch = uniq[i:i + 32]
        for _ in range(6):
            a, b = _embed_batch(batch), _embed_batch(batch)
            if all(cos(x, y) > 0.999 for x, y in zip(a, b)) and len(a) == len(batch):
                break
        else:
            raise RuntimeError("embeddings kept disagreeing between two identical requests")
        vec.update(zip(batch, a))
    return [vec[t or " "] for t in texts]


def cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def _mean(xs):
    xs = [float(x) for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


# ------------------------------------------------------------------ grade
pk_c, graphs, verdict = Counter(), defaultdict(Counter), Counter()
desc_rows, per_db = [], {}
for db in dbs:
    pks, fks = gold(db)
    keys = json.loads((keys_dir / f"{db}.json").read_text())
    for t, k in keys["tables"].items():
        rt = back(db, t).lower()
        g = pks.get(rt, [])
        pred = back(db, t, k["primary_key"])[1].lower() if k["primary_key"] else None
        if k.get("evidence") == "name_only":
            pred = None
        pk_c["predicted"] += pred is not None
        if len(g) == 1:
            pk_c["single"] += 1
            pk_c["correct"] += pred == g[0]

    jpath = s3_dir / "joins" / f"{db}.json"
    joins = json.loads((jpath if jpath.exists() else joins_dir / f"{db}.json").read_text())
    und_gold = {frozenset(p) for p in fks}

    def norm_edge(e):
        s, t = back(db, e["source"]["table"], e["source"]["column"]), back(db, e["target"]["table"], e["target"]["column"])
        return frozenset(((s[0].lower(), s[1].lower()), (t[0].lower(), t[1].lower())))

    db_g = {}
    for name in ("direct", "stats", "llm"):
        if name not in joins["graphs"]:
            continue
        edges = {norm_edge(e) for e in joins["graphs"][name]}
        tp = len(edges & und_gold)
        graphs[name].update(tp=tp, predicted=len(edges), gold=len(und_gold))
        db_g[name] = prf(tp, len(edges), len(und_gold))
    for r in joins["candidates"]:
        if r["decision"].startswith("llm_") or r["decision"] == "band":
            truth = norm_edge(r) in und_gold
            verdict[(r["decision"], truth)] += 1
    per_db[db] = {"joins": db_g, "gold_fks": len(und_gold)}

    dpath = s3_dir / f"{db}.json"
    if dpath.exists():
        refs = ref_descriptions(db)
        for t, td in json.loads(dpath.read_text())["tables"].items():
            for c, cd in td["columns"].items():
                rt, rc = back(db, t, c)
                desc_rows.append({"db": db, "table": rt, "column": rc, "status": cd["status"], "role": cd["role"],
                                  "pred": " ".join(x for x in (cd["name"], cd["description"]) if x),
                                  "ref": refs.get((rt.lower(), rc.lower()), ""), "ok": td.get("ok", True)})

report = {"label": label,
          "primary_keys": prf(pk_c["correct"], pk_c["predicted"], pk_c["single"]),
          "joins": {g: prf(c["tp"], c["predicted"], c["gold"]) for g, c in graphs.items()},
          "band_verdicts": {f"{d}|{'gold' if t else 'not_gold'}": n for (d, t), n in sorted(verdict.items())},
          "per_db": per_db}

if desc_rows:
    graded = [r for r in desc_rows if r["ref"]]
    with_text = [r for r in graded if r["pred"]]
    embs = embed([r["pred"] for r in with_text] + [r["ref"] for r in with_text]) if with_text else []
    n = len(with_text)
    by_table = defaultdict(list)
    for i, r in enumerate(with_text):
        by_table[(r["db"], r["table"])].append(i)
    for i, r in enumerate(with_text):
        r["cosine"] = round(cos(embs[i], embs[n + i]), 4)
        r["token_f1"] = round(token_f1(r["pred"], r["ref"]), 4)
        # chance level: the same description against the next column's reference in the same table
        peers = by_table[(r["db"], r["table"])]
        if len(peers) > 1:
            j = peers[(peers.index(i) + 1) % len(peers)]
            r["cosine_other_column"] = round(cos(embs[i], embs[n + j]), 4)
            r["rank_in_table"] = 1 + sum(cos(embs[i], embs[n + k]) > r["cosine"] for k in peers if k != i)
            r["table_columns"] = len(peers)
    by_status = {}
    for s in ("resolved", "tentative", "unresolved"):
        rs = [r for r in graded if r["status"] == s]
        sc = [r for r in rs if "cosine" in r]
        by_status[s] = {"columns": len(rs),
                        "mean_cosine": round(sum(r["cosine"] for r in sc) / len(sc), 4) if sc else None,
                        "mean_token_f1": round(sum(r["token_f1"] for r in sc) / len(sc), 4) if sc else None}
    report["descriptions"] = {
        "columns": len(desc_rows), "with_reference": len(graded),
        "status": dict(Counter(r["status"] for r in desc_rows)),
        "failed_table_columns": sum(not r["ok"] for r in desc_rows),
        "mean_cosine_described": round(sum(r["cosine"] for r in with_text) / n, 4) if n else None,
        "mean_token_f1_described": round(sum(r["token_f1"] for r in with_text) / n, 4) if n else None,
        "mean_cosine_other_column": _mean(r.get("cosine_other_column") for r in with_text),
        "own_reference_ranked_first": _mean(r["rank_in_table"] == 1 for r in with_text if "rank_in_table" in r),
        "by_status": by_status}
    (s3_dir / "grade_columns.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in desc_rows) + "\n")
(s3_dir / "grade.json").write_text(json.dumps(report, indent=1))
print(json.dumps({k: v for k, v in report.items() if k != "per_db"}, indent=1))
