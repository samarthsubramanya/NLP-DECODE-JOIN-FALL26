# Fixing the main result: BEAVER joins and walk losses, 2026-11-06

Two changes, both opt-in. Validated on BEAVER, FIBEN and BIRD for the repair, and on all five benchmarks for the merge. Raw rows are in `out/eval/repair/`. Scores use bge. Baseline numbers are in [ddb_eval_2026-11-05.md](ddb_eval_2026-11-05.md).

## Why BEAVER's walk failed

64 of the 120 questions needed a gold table that had no join at all in our graph (`MOIRA_LIST`, `EMPLOYEE_DIRECTORY`, `SE_PERSON` and others). 20 of the 92 dw tables with rows were isolated. The walk can't reach an isolated table, whatever the quality of the other joins.

The cause is the data. Most large dw tables hold exactly 10,000 rows, and they appear to have been cut independently. `MOIRA_LIST.MOIRA_LIST_KEY` and `MOIRA_LIST_DETAIL.MOIRA_LIST_KEY` share 1,498 key values, but that's only 17–23% containment and neither column is unique. Stage 1 only compares columns against a unique, null-free target at 50% or more, so the pair was never considered.

## Change 1: repair isolated tables

- `profile_columns.py --overlaps` writes shared-value counts for all same-family column pairs to `out/overlaps/`, with no key needed.
- `foreign_keys.py --repair-isolated 2` gives each table with rows and no join up to 2 extra candidates, ranked by name similarity (if names are used), share and number of shared values. Dense integer columns are left out.
- All of them go to the LLM band, with a note that low containment may come from truncated tables. Nothing is auto-accepted.

Without the flag, Stage 2b output is identical to before (checked on dw). The note only appears on repair records, so other prompts and their cache entries don't change.

| BEAVER dw walk | Before | After |
|---|---|---|
| PR@15, named and stripped | .183 | **.383** |
| PR@15, obfuscated | .167 | **.300** |
| R@15, named and stripped | .559 | .713 |
| R@15, obfuscated | .544 | .665 |
| Column R@15, named and stripped | .179 | .236 |
| Column R@15, obfuscated | .191 | .246 |
| Join edges, named | 344 | 357 |

Only 13 net edges were added on named: it's which tables they reach that matters. The walk now beats dense on the same graph (.383 against .367) on named and stripped. The LLM rejected the shared-first-name and shared-year candidates and accepted the real keys, such as `MOIRA_LIST_KEY`, `ISBN` and `LIBRARY_RESERVE_CATALOG_KEY`.

**Regression check:**

| Run | Join F1 before → after | Walk PR@15 before → after |
|---|---|---|
| BIRD, named and obfuscated | .892 → .892, .813 → .813 | not rescored (same edge counts) |
| FIBEN named | .633 → .626 | .840 → .843 |
| FIBEN obfuscated | .672 → .672 | .620 → .620 |
| BEAVER all, named | .233 → .242 | |
| BEAVER all, obfuscated | .161 → .165 | |
| BEAVER neutron, named | .310 → .307 | not scored (no JoinGR graph) |
| BEAVER nova, named | .281 → .281 | not scored (no JoinGR graph) |

Join F1 barely moves because dw's gold is JoinGR's edge list, and the fix targets reachability. Neutron's misses are empty tables, which have no values to compare, so the repair can't help them.

## Change 2: stop the walk losing what dense finds

`merged` in `baselines/run.py` keeps the walk's top 13 and gives the last 2 of the top 15 to the best dense tables the walk missed. Reciprocal rank fusion was tried first and dropped, because it took FIBEN stripped from .850 to .587.

| Benchmark | Variant | Dense alone | Walk | Merged | Walk rescued / lost | Merged rescued / lost |
|---|---|---|---|---|---|---|
| FIBEN | Stripped | .053 | .850 | .813 | 240 / 1 | 229 / 1 |
| FIBEN | Obfuscated | .027 | .620 | .593 | 178 / 0 | 170 / 0 |
| BIRD | Stripped | .949 | .950 | **.988** | 47 / 45 | 48 / 2 |
| BIRD | Obfuscated | .893 | .951 | **.985** | 110 / 42 | 111 / 4 |
| BEAVER dw (repaired) | Stripped | .308 | .383 | **.408** | 18 / 9 | 18 / 6 |
| BEAVER dw (repaired) | Obfuscated | .158 | .300 | **.350** | 20 / 3 | 26 / 3 |
| Spider | Stripped | .998 | .983 | **1.000** | 1 / 8 | 1 / 0 |
| Spider | Obfuscated | .996 | .908 | **1.000** | 1 / 41 | 2 / 0 |
| KaggleDBQA | Both | 1.000 | 1.000 | 1.000 | 0 / 0 | 0 / 0 |

Merged beats dense alone everywhere it can, and cuts losses from 149 to 16 questions in total across these runs. The cost is about 3–4 points on FIBEN, where dense is weak and those 2 slots are wasted.

## Caveats

- **Tuned on the test questions.** The 2 dense slots were picked from 2, 3, 5 and 7 on these same questions, with no held-out check. The repair's thresholds and K = 2 were set on dw; the truncation test below is its held-out check.
- **JoinGR's .492 isn't a clean bar.** Its dw graph contains 100 of the 102 joins used by the evaluation questions themselves. No such information reaches our pipeline, so .383–.408 from data alone is less far behind than it looks. How that graph was built should be confirmed before saying so in the paper.
- **dw precision is unknown.** Join "false positives" are measured against that same edge list, so many may be real joins.
- **BEAVER is improved, not solved.** Most questions there still fail.
- **One run each, bge only for merged.** Spider and KaggleDBQA weren't run with the repair. Both have few or no isolated tables expected, but that's unchecked.

## Does the repair generalize? Truncation test

`evals/truncate_tables.py` cuts every table independently to a share of its rows, recreating BEAVER's condition on BIRD and Spider. The repair's settings were not changed. Both arms use Stage 3 joins with no descriptions (`joins_only` export), one run per cell. Results are in `out/eval/truncation/`.

Join F1 and walk PR@15, without → with the repair:

| Dataset | Complete data (F1, walk) | 50% rows, seed 0 | 50% rows, seed 1 | 30% rows |
|---|---|---|---|---|
| BIRD named | .892, .954 | F1 0 → .297, walk 0 → .438 | F1 0 → .286, walk 0 → .511 | F1 0 → .228, walk 0 → .448 |
| BIRD obfuscated | .813, .945 | F1 0 → .241, walk 0 → .471 | F1 0 → .239, walk 0 → .537 | F1 0 → .196, walk 0 → .477 |
| Spider named | .889, .983 | F1 .083 → .208, walk .083 → .240 | F1 .104 → .198, walk .065 → .222 | F1 0 → .113, walk .009 → .209 |
| Spider obfuscated | .667, .908 | F1 .081 → .175, walk .109 → .266 | F1 .074 → .161, walk .074 → .231 | F1 0 → .107, walk .009 → .222 |

- **It generalizes in direction.** The repair helped in all 12 cells, with settings fixed on BEAVER. So it isn't a BEAVER-only fix.
- **It only partly fixes truncation.** Recall stays at .06–.21 and precision at .29–.57. The walk stays far below complete data, and far below dense (.90–1.00), which truncation barely touches.
- **The baseline comparison is an easy one.** Without the repair, truncated BIRD has 0–2 edges, so the walk starts from almost nothing.
- **Likely limits, not yet tested:** the repair only acts on tables with no join at all, then stops, so one edge per table is often all it adds. The floor of 10 shared values probably blocks many of Spider's small tables.
- **Merged ranking:** with the repair it scores .910–.962 on BIRD, 1 to 2 points above no repair. Spider stays at .996–1.000, with two cells down .002.

**The bigger finding for RQ3:** without the repair, cutting tables to 50% takes join F1 from .892 to 0 on BIRD, because child values lose their parents and fall under the G6 gate. That cliff comes from the fixed .75 containment gate, and the repair only patches around it.
