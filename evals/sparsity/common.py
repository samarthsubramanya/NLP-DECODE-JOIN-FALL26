"""Shared helpers for the sparsity sweep (hp). Nothing here changes a pipeline stage; it only calls them.

Paths, the work list, gold joins (through evals/grade_joins_all.load_gold, unchanged), where a benchmark's DuckDB
file lives, and precision / recall / F1.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "src", ROOT / "evals", ROOT / "evals" / "sparsity"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

BENCHMARKS = ("bird", "spider", "fiben", "beaver", "kaggledbqa")
SETTINGS = ("stripped", "obfuscated")
OUT = ROOT / "out" / "sparsity"


def documented(dataset):
    return dataset.removesuffix("_obfuscated").removesuffix("_stripped")


def uses_names(dataset):
    """Obfuscated datasets run Stage 2a/2b with --no-names, as the README says."""
    return not dataset.endswith("_obfuscated")


def work_list(dataset, databases=None):
    dbs = json.loads((ROOT / "out" / "filtered" / dataset / "databases.json").read_text())
    return [d for d in dbs if not databases or d["db_id"] in databases]


def source_file(filename):
    """Where src/duckdb_driver.open_db would read `filename` from: $DECODE_JOIN_DUCKDB_DIR, ../data/..., else the
    Hugging Face cache (downloaded once if missing). Returned as a path so it can be ATTACHed read-only."""
    from duckdb_driver import LOCAL_DIR, REPO
    local = Path(os.environ.get("DECODE_JOIN_DUCKDB_DIR") or LOCAL_DIR) / filename
    if local.is_file():
        return local
    from huggingface_hub import hf_hub_download
    return Path(hf_hub_download(repo_id=REPO, filename=filename, repo_type="dataset"))


def prf(tp, predicted, gold):
    p = tp / predicted if predicted else 0.0
    r = tp / gold if gold else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"tp": tp, "predicted": predicted, "gold": gold, "false_positives": predicted - tp,
            "precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4)}


class Gold:
    """Gold joins of one dataset variant as undirected pairs of real, lower-case (table, column), plus the name
    mapping that turns a record of this variant into that form. Built once per dataset (opens the documented file)."""

    def __init__(self, dataset):
        import labels
        from grade_joins_all import load_gold
        self.dataset, self.base = dataset, documented(dataset)
        try:
            self.schemas, self.mapping, self._gold = load_gold(dataset)
        except FileNotFoundError as e:
            fix = ""
            if self.base == "kaggledbqa":
                fix = "python -m baselines.build_graph --dataset kaggledbqa && "
            raise SystemExit(f"{dataset}: gold joins need {e.filename}. For BEAVER dw and KaggleDBQA they come from the "
                             f"documented JoinGR graph: {fix}python -m baselines.settings --dataset {self.base} "
                             f"--setting documented stripped") from None
        self.labels = labels

    def pairs(self, db, profile=None):
        """Gold pairs for db. FIBEN keeps only FKs whose two tables have rows in `profile`, as grade_joins_all does."""
        fks = set(self._gold(db))
        if self.base == "fiben" and profile is not None:
            rows = {t.lower(): p["rows"] for t, p in profile["tables"].items()}
            fks = {f for f in fks if all(rows.get(t, 0) for t, _ in f)}
        return fks

    def norm(self, db, edge):
        return self.labels.norm_edge(self.mapping, db, edge)

    def shown(self, db, table, column):
        """real (table, column) -> the names this variant's DuckDB file uses (col_NNN when obfuscated)."""
        if not self.mapping:
            return table, column
        m = self.mapping[db]
        t = {k.lower(): v for k, v in m["tables"].items()}[table.lower()]
        real_t = next(k for k, v in m["tables"].items() if v == t)
        return t, {k.lower(): v for k, v in m["columns"][real_t].items()}[column.lower()]


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.lstrip().startswith("{")]


def append_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
