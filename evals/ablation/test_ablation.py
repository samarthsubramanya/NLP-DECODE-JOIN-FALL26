"""Tests for the ablation tools only. No database, no LLM, no files outside a temp folder.

    python -m pytest evals/ablation -q
"""
import importlib
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for mod in ("duckdb", "pandas", "huggingface_hub"):      # only needed to open real databases, never here
    try:
        importlib.import_module(mod)
    except ImportError:
        stub = types.ModuleType(mod)
        stub.hf_hub_download = None
        stub.DuckDBPyConnection = object
        sys.modules[mod] = stub
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "evals" / "ablation"), str(ROOT / "src")]

import grade                                              # noqa: E402
import table                                              # noqa: E402
import variants                                           # noqa: E402
import test_foreign_keys as tfk                           # noqa: E402  (reuses its toy profiles)
from profile_columns import quantile_emd                  # noqa: E402

KEY = (("orders", "customer_id"), ("customers", "customer_id"))


def toy(dist):
    profile, keys = tfk.shop(use_names=False)
    profile["containment"] = [tfk.pair(*KEY, dist=dist)]
    return profile, keys


def test_strip_distribution_is_a_copy_and_clears_every_pair():
    profile, _ = toy(0.2)
    stripped = variants.strip_distribution(profile)
    assert all(p["dist"] is None and p["dist_buckets"] is None for p in stripped["containment"])
    assert profile["containment"][0]["dist"] == 0.2                      # the cached profile is untouched


def test_no_qmd_moves_distribution_points_to_overlap_and_drops_the_cap():
    """A 1-3 measure in an id range: FULL caps it into the LLM band; without QuantileMD it is auto-accepted."""
    profile, keys = toy(quantile_emd([3] + [0] * 9))
    profile["tables"]["orders"]["columns"]["customer_id"]["distinct"] = 3
    full = tfk.one(tfk.run(profile, keys), *KEY)
    noqmd = tfk.one(tfk.run(variants.strip_distribution(profile), keys), *KEY)
    assert full["decision"] == "band" and full["dist_capped"] is True
    assert noqmd["decision"] == "accepted" and "dist_capped" not in noqmd
    assert noqmd["weights_used"]["distribution"] == 0
    assert noqmd["weights_used"]["overlap"] == full["weights_used"]["overlap"] + full["weights_used"]["distribution"]


def test_no_qmd_equals_full_when_the_pair_has_no_distribution_evidence():
    profile, keys = toy(None)
    assert tfk.run(profile, keys) == tfk.run(variants.strip_distribution(profile), keys)


def test_swap_replaces_llm_graph_keeps_the_original_and_leaves_input_alone():
    joins = {"graphs": {"direct": [1], "stats": [1, 2, 3], "llm": [1, 2]}, "candidates": []}
    out = variants.swap(joins, "stats")
    assert out["graphs"]["llm"] == [1, 2, 3] and out["graphs"]["llm_full"] == [1, 2] and out["ablation"] == "graph=stats"
    assert joins["graphs"]["llm"] == [1, 2] and "llm_full" not in joins["graphs"]
    try:
        variants.swap({"graphs": {"llm": []}}, "direct")
        raise AssertionError("missing graph should raise")
    except KeyError:
        pass


def test_fresh_dir_refuses_existing_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(variants, "ROOT", tmp_path)
    variants.fresh_dir(tmp_path / "new")
    try:
        variants.fresh_dir(tmp_path / "new")
        raise AssertionError("existing folder must be refused")
    except SystemExit:
        pass


def test_prf_counts_false_positives():
    assert grade.prf(3, 8, 6) == {"tp": 3, "predicted": 8, "gold": 6, "false_positives": 5,
                                  "precision": 0.375, "recall": 0.5, "f1": 0.4286}
    assert grade.prf(0, 0, 0)["f1"] == 0.0


def test_parse_config():
    assert grade.parse_config("A2=fiben_stripped:ddb_low:stats") == {
        "name": "A2", "dataset": "fiben_stripped", "run": "ddb_low", "graph": "stats"}


def test_edge_rows_mark_false_positives_and_llm_verdicts():
    rec = lambda s, t, d, llm=None: {"source": {"table": s, "column": "c"}, "target": {"table": t, "column": "id"},  # noqa: E731
                                     "decision": d, "score": 65, **({"llm": llm} if llm else {})}
    cands = [rec("a", "b", "accepted"), rec("a", "x", "llm_accept", {"decision": "accept", "reason": "r"}),
             rec("a", "y", "llm_reject", {"decision": "reject", "reason": "r"})]
    norm = lambda e: frozenset(((e["source"]["table"], e["source"]["column"]), (e["target"]["table"], e["target"]["column"])))  # noqa: E731
    chosen = {norm(c) for c in cands[:2]}
    fks = {norm(cands[0])}
    rows = list(grade.edge_rows("A0", "toy", "db", {"candidates": cands}, "llm", chosen, fks, norm))
    assert [r["is_false_positive"] for r in rows] == [False, True, False]
    assert [r["llm_decision"] for r in rows] == [None, "accept", "reject"]
    assert rows[0]["gold_edge"] and not rows[2]["edge_accepted"]


def test_render_shows_change_from_full():
    j = lambda tp, n: {**grade.prf(tp, n, 10)}                            # noqa: E731
    r = lambda pr: {"scores": {"bge/heuristic": {"PR@15": pr, "colR@15": 0.5}}}   # noqa: E731
    md = table.render([{"name": "A0", "joins": j(8, 10), "retrieval": r(0.9)},
                       {"name": "A1", "joins": j(8, 20), "retrieval": r(0.7)}])
    last = md.splitlines()[-1]
    assert "| A1 |" in last and "+10" in last and "-0.200" in last


def test_committed_table_matches_the_saved_ddb_results():
    rows = table.committed("bird_stripped")
    assert table.fp(rows[0]["joins"]) == 19 and table.fp(rows[1]["joins"]) == 99


def test_from_stage2b_copies_the_chosen_graph_without_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(variants, "ROOT", tmp_path)
    src = tmp_path / "out/joins/toy/r2b"
    src.mkdir(parents=True)
    (src / "db.json").write_text(json.dumps({"graphs": {"direct": [1], "stats": [1, 2]}, "keys_run": "k"}))
    variants.from_stage2b(["--dataset", "toy", "--joins-run", "r2b", "--graph", "direct", "--run", "x"])
    out = json.loads((tmp_path / "out/descriptions/toy/x/joins/db.json").read_text())
    assert out["graphs"]["llm"] == [1] and out["keys_run"] == "k"


def test_read_jsonl_skips_progress_lines(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text('fiben/x/bge: 374 nodes -> /tmp\n{"run": "a"}\nBatches: 100%\n')
    assert table.read_jsonl(p) == [{"run": "a"}]
