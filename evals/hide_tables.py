"""Memorization check: BIRD with column AND table names hidden (bird_dev_obfuscated.duckdb hides columns only).

Copies src/, poc/, .env and the bird_obfuscated Stage 1-2b outputs (profiles, keys and joins frozen_no_names) into
<dst>, renaming tables t01, t02, ... (sorted order) in every key and string. Database ids stay (Stage 3 never sends
them). Writes <dst>/mapping.json. Then, in <dst>:
    python src/describe_columns.py --dataset bird_obfuscated --joins-run frozen_no_names --run hidden_tables_low \
        --reasoning-effort low --workers 8 --adjudication single
and map the output back to real table names with mapping.json before grading with evals/grade_bird.py.

Usage: python evals/hide_tables.py <repo> <dst>
"""
import json, re, shutil, sys
from pathlib import Path
R, D = Path(sys.argv[1]), Path(sys.argv[2])
for d in ("src", "poc"):
    shutil.copytree(R / d, D / d, dirs_exist_ok=True)
(D / ".env").write_text((R / ".env").read_text())
dbs = json.loads((R / "out/filtered/bird_obfuscated/databases.json").read_text())
mapping = {}
def sub(x, tm):
    if isinstance(x, dict):
        return {sub(k, tm): sub(v, tm) for k, v in x.items()}
    if isinstance(x, list):
        return [sub(v, tm) for v in x]
    if isinstance(x, str):
        if x in tm:
            return tm[x]
        m = re.fullmatch(r"([^.]+)\.(col_\d+)", x)
        if m and m[1] in tm:
            return f"{tm[m[1]]}.{m[2]}"
    return x
out_dbs = []
for db in dbs:
    tm = {t: f"t{i + 1:02d}" for i, t in enumerate(sorted(db["tables"]))}
    mapping[db["db_id"]] = tm
    out_dbs.append({**sub(db, tm), "db_id": db["db_id"], "dataset": db["dataset"]})   # a db can share a table name
    for rel in (f"out/profiles/bird_obfuscated/{db['db_id']}.json",
                f"out/keys/bird_obfuscated/frozen_no_names/{db['db_id']}.json",
                f"out/joins/bird_obfuscated/frozen_no_names/{db['db_id']}.json"):
        p = D / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        old = json.loads((R / rel).read_text())
        new = {**sub(old, tm), **{k: old[k] for k in ("db_id", "dataset") if k in old}}
        p.write_text(json.dumps(new, indent=1))
        left = [t for t in tm if t != db["db_id"] and re.search(r'"' + re.escape(t) + r'[".]', p.read_text())]
        assert not left, (rel, left)
(D / "out/filtered/bird_obfuscated").mkdir(parents=True, exist_ok=True)
(D / "out/filtered/bird_obfuscated/databases.json").write_text(json.dumps(out_dbs, indent=1))
(D / "mapping.json").write_text(json.dumps(mapping, indent=1))
print("ok", {k: len(v) for k, v in mapping.items()})
