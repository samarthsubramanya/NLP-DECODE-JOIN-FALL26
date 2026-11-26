# Component ablation (hp) — preliminary, 2026-11-11

Owner: hp. Code: `evals/ablation/` (new files only; no shared pipeline file is changed).

**Question:** which components reduce false-positive join edges, and does that carry through to JoinGR retrieval?
Chain measured: component → join graph (P / R / F1, edges, false positives) → JoinGR walk (table PR@15, column R@15).

## Configurations

| ID | Config | How it is produced |
|---|---|---|
| A0 | FULL | team runs `ddb_low` (`graphs.llm`) / `ddb_full` |
| A1 | − QuantileMD | `variants.py no-qmd`: Stage 2b on profiles with `dist` removed in memory → Stage 3 → Stage 4 |
| A2 | − LLM verification | `graphs.stats` (band accepted without the LLM); `variants.py swap-graph --graph stats` for export |
| A3 | − Column descriptions | team export `ddb_joins_only` (`--no-descriptions`) |
| A4 | Heuristics only | `graphs.direct` + `--no-descriptions` |

Each differs from FULL in one component; data, questions, LLM (glm-5-3, low), prompts, cache and JoinGR settings are shared.
"Downstream accuracy" here is JoinGR retrieval (bge); the repo has no SQL-execution QA.

## Preliminary results from saved runs (no new runs)

Built with `python evals/ablation/table.py --committed <dataset>` from `out/eval/ddb/`. A1 and A4 need the runner;
A2 retrieval needs its export. Walk = JoinGR heuristic walk.

### bird_stripped

| Config | Edge P | Edge R | Edge F1 | Edges | False pos. | Δ F1 | Δ FP | Walk PR@15 | Δ Walk | Merged PR@15 | Col R@15 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A0 FULL | 0.839 | 0.952 | 0.892 | 118 | 19 |  |  | 0.950 |  | – | 0.615 |
| A2 − LLM verification | 0.500 | 0.952 | 0.656 | 198 | 99 | -0.236 | +80 | – |  | – | – |
| A3 − Column descriptions | 0.839 | 0.952 | 0.892 | 118 | 19 | +0.000 | +0 | 0.954 | +0.003 | – | 0.596 |

### bird_obfuscated

| Config | Edge P | Edge R | Edge F1 | Edges | False pos. | Δ F1 | Δ FP | Walk PR@15 | Δ Walk | Merged PR@15 | Col R@15 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A0 FULL | 0.704 | 0.962 | 0.813 | 142 | 42 |  |  | 0.951 |  | – | 0.583 |
| A2 − LLM verification | 0.188 | 0.962 | 0.315 | 532 | 432 | -0.498 | +390 | – |  | – | – |
| A3 − Column descriptions | 0.704 | 0.962 | 0.813 | 142 | 42 | +0.000 | +0 | 0.945 | -0.006 | – | 0.494 |

### fiben_stripped

| Config | Edge P | Edge R | Edge F1 | Edges | False pos. | Δ F1 | Δ FP | Walk PR@15 | Δ Walk | Merged PR@15 | Col R@15 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A0 FULL | 0.620 | 0.646 | 0.633 | 50 | 19 |  |  | 0.850 |  | – | 0.490 |
| A2 − LLM verification | 0.576 | 0.708 | 0.635 | 59 | 25 | +0.003 | +6 | – |  | – | – |
| A3 − Column descriptions | 0.620 | 0.646 | 0.633 | 50 | 19 | +0.000 | +0 | 0.860 | +0.010 | – | 0.369 |

### fiben_obfuscated

| Config | Edge P | Edge R | Edge F1 | Edges | False pos. | Δ F1 | Δ FP | Walk PR@15 | Δ Walk | Merged PR@15 | Col R@15 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A0 FULL | 0.563 | 0.833 | 0.672 | 71 | 31 |  |  | 0.620 |  | – | 0.390 |
| A2 − LLM verification | 0.432 | 0.854 | 0.573 | 95 | 54 | -0.099 | +23 | – |  | – | – |
| A3 − Column descriptions | 0.563 | 0.833 | 0.672 | 71 | 31 | +0.000 | +0 | 0.427 | -0.193 | – | 0.196 |

### kaggledbqa_stripped

| Config | Edge P | Edge R | Edge F1 | Edges | False pos. | Δ F1 | Δ FP | Walk PR@15 | Δ Walk | Merged PR@15 | Col R@15 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A0 FULL | 0.375 | 0.500 | 0.429 | 8 | 5 |  |  | 1.000 |  | – | 0.862 |
| A2 − LLM verification | 0.231 | 0.500 | 0.316 | 13 | 10 | -0.113 | +5 | – |  | – | – |
| A3 − Column descriptions | 0.375 | 0.500 | 0.429 | 8 | 5 | +0.000 | +0 | 1.000 | +0.000 | – | 0.825 |

## What this already shows

- **LLM verification is the main false-positive filter.** Without it, false edges rise by 80 on BIRD stripped (19 → 99)
  and by 390 on BIRD obfuscated (42 → 432); F1 drops .236 and .498. On FIBEN stripped it is nearly neutral
  (−6 false edges but −3 true ones, F1 ±.003).
- **Column descriptions matter only when names disappear.** Removing them changes the walk by ≤ .01 in every stripped
  setting but costs .193 PR@15 on FIBEN obfuscated (.620 → .427) and ~.1–.2 column R@15 in obfuscated settings.
  Joins are identical (descriptions are written after joins), so the effect is purely on retrieval.
- **QuantileMD (prior evidence, Stage 2b only):** the 2026-11-04 leave-one-out in `docs/dist_eval_2026-11-04.md`
  reports auto-accept F1 −.511 on BIRD obfuscated without the distribution term. A1 here repeats it through the LLM
  and JoinGR so the effect on retrieval is measured, not estimated.

## Result 1 — FIBEN stripped, no LLM (2026-11-12)

Stage 1–2b rerun locally; FULL reproduces the saved `ddb` numbers exactly (59 edges, 25 false, F1 .635).
Graphs exported with `--no-descriptions` (zero LLM calls); retrieval = bge, 300 questions.

| Config | Edge P | Edge R | Edge F1 | Edges | False pos. | Walk PR@15 | Merged PR@15 | Col R@15 |
|---|---|---|---|---|---|---|---|---|
| FULL, band accepted (no LLM) | .576 | .708 | .635 | 59 | 25 | .847 | .847 | .369 |
| FULL, heuristics only | .750 | .312 | .441 | 20 | 5 | .277 | .380 | .370 |
| −QuantileMD, band accepted | .434 | .750 | .550 | 83 | 47 | .837 | .823 | .369 |
| −QuantileMD, heuristics only | .567 | .354 | .436 | 30 | 13 | .730 | .820 | .369 |

For reference, the team's FULL run with LLM verification and descriptions (`ddb_full`) scores .850 walk PR@15.

- **QuantileMD cuts false joins nearly in half** (47 → 25) when the band is accepted, but the walk moves only .010:
  on FIBEN, JoinGR's walk tolerates extra wrong edges.
- **Without a verifier, QuantileMD costs retrieval.** Heuristics-only walk PR@15 is .277 with it and .730 without.
  Two true joins explain it, both to hub tables many questions pass through:
  - `LISTEDSECURITY.HASLASTTRADEDVALUE → MONETARYAMOUNT.MONETARYAMOUNTID`: distribution similarity 0.0, so the
    low-distribution cap holds it in the band (61.5). A child column that references one slice of a shared ID range
    looks "different" by quantiles even when the join is real.
  - `CORPORATION.ISDOMICILEDIN → COUNTRY.COUNTRYID`: distribution 0.9 (a good match), yet the score falls from
    70.6 to 69.3, just under the auto-accept line (70). A threshold-edge effect, not a mismatch.
- **Implication:** QuantileMD is a precision filter that relies on a downstream check (LLM or band acceptance) to
  recover the true joins it demotes. Shared-ID-range ("hub") targets are its main false-negative pattern.
- **Open:** A1 with LLM verification (needs Voyager) tests whether the LLM recovers those two joins.

## Result 2 — BIRD stripped, no LLM (2026-11-12)

FULL reproduces the saved `ddb` numbers (198 edges, 99 false). 1171 questions, bge.

| Config | Edge P | Edge R | Edge F1 | Edges | False pos. | Walk PR@15 | Merged PR@15 | Col R@15 |
|---|---|---|---|---|---|---|---|---|
| FULL, band accepted (no LLM) | .500 | .952 | .656 | 198 | 99 | .950 | .986 | .594 |
| FULL, heuristics only | .835 | .731 | .779 | 91 | 15 | .845 | .983 | .563 |
| −QuantileMD, band accepted | .162 | .952 | .277 | 612 | 513 | .949 | .985 | .591 |
| −QuantileMD, heuristics only | .561 | .798 | .659 | 148 | 65 | .890 | .985 | .572 |

Team FULL with LLM verification + descriptions (`ddb_full`): 118 edges, 19 false, walk .950.

- **Without QuantileMD the graph explodes:** 198 → 612 edges, false joins 99 → 513 (5×), F1 .656 → .277.
  Same recall (.952), so every added edge is wrong. On FIBEN the same removal added 22 false joins; BIRD's many
  integer ID columns with overlapping ranges are where value overlap alone cannot tell joins apart.
- **Retrieval does not show it on BIRD stripped:** walk .950 → .949. Dense search alone already scores .948 here
  (column names are kept in the stripped setting), so the walk has little to add or lose. Retrieval sensitivity to
  false joins has to be tested where names are gone (obfuscated).
- **Same heuristics-only pattern as FIBEN:** QuantileMD trades recall (.798 → .731) for precision (65 → 15 false),
  and with no verifier the walk pays for it (.890 → .845).

## Summary so far

| | FIBEN stripped | BIRD stripped |
|---|---|---|
| False joins added by removing QuantileMD (band accepted) | +22 (25 → 47) | +414 (99 → 513) |
| Walk PR@15 change from removing QuantileMD (band accepted) | −.010 | −.002 |
| Walk PR@15 change from removing QuantileMD (heuristics only) | **+.453** | **+.045** |

QuantileMD is the main guard against graph explosion. It depends on a later check (LLM or band acceptance) to
recover the true joins it demotes; on its own it loses joins to hub tables. Next: obfuscated settings, where
names are gone and retrieval should depend on the graph.

## Run the full ablation

```bash
python -m pytest evals/ablation -q                  # no data, no LLM
bash evals/ablation/run_ablation.sh fiben_stripped  # needs the team's ddb runs, the DuckDB files, VOYAGER_API_KEY
# no-LLM path: variants.py from-stage2b + export_joingr.py --no-descriptions (see Result 1)
bash evals/ablation/run_ablation.sh bird_stripped
```

Outputs: `out/ablation/<dataset>/joins.jsonl`, `edges_<config>.jsonl` (one row per candidate: score parts, `dist`,
decision, LLM verdict, `edge_accepted`, `gold_edge`, `is_false_positive`), `retrieval.jsonl`, `table.md`.

## Safety

- New files only: `evals/ablation/*`, this doc. `src/`, `poc/`, `baselines/`, `tests/` are unchanged.
- New run names only (`abl_*`); `variants.py` refuses to write into an existing folder. A0/A3 retrieval is read from
  the saved `out/eval/ddb/retrieval.jsonl`, not re-scored.
- Reverting = deleting `evals/ablation/` and this file.

## Next

1. Run A1–A4 on FIBEN stripped and BIRD stripped; fill the table.
2. Trace 10–20 false edges from `edges_A0_full.jsonl` vs `edges_A1_noqmd.jsonl` / `edges_A2_nollm.jsonl` into the walk.
3. Extend to obfuscated, then the other benchmarks.
