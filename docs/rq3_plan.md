# RQ3 plan: sparsity and abstention

Written 2026-11-06. Nothing here has been run or built yet, apart from the read-only analysis in section 2.

**RQ3:** how fast does join recovery degrade on sparse columns, and does abstaining stop false joins?

**Ground rules:** use `data/decode-join-duckdb` only. Do not commit. Do not overwrite existing `out/` runs, and do not re-run the filter (it removes the 4 hand-appended KaggleDBQA databases). Python: `code/decode-join/.venv/bin/python`.

**Update 2026-11-06:** `--repair-isolated` ([beaver_repair_2026-11-06.md](beaver_repair_2026-11-06.md)) now handles partial overlap for isolated tables. Include it as a variant in the `parent_delete` and `row_sample` sweeps. The BEAVER note in section 4 predates it.

## 1. What the code does with sparse data today

| Situation | Code | Effect |
|---|---|---|
| One null in a key | `key_like` (`profile_columns.py:221`) needs `nulls == 0` and `distinct == rows`; `hard_reject` (`primary_keys.py:217`) rejects any null | The column can't be a target, so **one null removes every join into it**. A cliff, not a slope. `--allow-nulls` relaxes 2a only, not `key_like` |
| Nulls in an FK source | Containment uses distinct non-null values (`profile_columns.py:273`) | Random nulls barely change containment. G6 is not what sparsity stresses here |
| Null term | `null_term` (`foreign_keys.py:163`) | Costs only 5 points at 30% nulls and 10 points at 70% |
| Cardinality term | `rho = rows / distinct` (`foreign_keys.py:249`) counts null rows | **Likely bug:** nulls lower `distinct`, so `r` goes up and can offset the null term |
| Empty table | `distinct > 0` for sources, `rows > 0` for targets (`profile_columns.py:223,270`); `foreign_keys.py` only loops over containment pairs | No pair exists, so the join is never considered. R5 gives the table a key, but nothing uses it for joins |
| Few distinct values | Only `min_source_distinct = 2`. No sample-size term | Containment of 2 of 2 values scores the same as 5,000 of 5,000 |
| No distribution evidence | `d = None` moves its points to overlap and skips the cap (`foreign_keys.py:261`) | Thin evidence can gain score |
| Missing parent rows | Orphan penalty x0.7 above 20%, G6 gate above 25% | This is the path that actually stresses containment |
| Abstain | `adjudicate` (`describe_columns.py:280`) only sees band records; `graphs.llm` drops abstained ones | Edges scoring 70+ can't be abstained on |

## 2. Baseline numbers from the existing `ddb` runs

Scripts: `/private/tmp/claude-501/-Users-samarthms-NLP/2f10857a-a2f9-4566-bf12-38c3dc574873/scratchpad/rq3/analyze.py` and `ceil.py`. "False" means not in gold. BEAVER dw gold is JoinGR's own edges, not declared FKs, so its false joins may be real joins.

**Why gold FKs are missed (named runs):**

| Benchmark | Gold | Found | Empty table | No key-like end | Containment / family | Gates | Score | LLM reject |
|---|---|---|---|---|---|---|---|---|
| BIRD | 104 | 99 | 0 | 1 | 1 | 2 | 1 | 0 |
| Spider | 63 | 60 | 0 | 1 | 1 | 1 | 0 | 0 |
| BEAVER | 385 | 108 | 111 | 117 | 19 | 29 | 1 | 0 |
| KaggleDBQA | 6 | 3 | 0 | 3 | 0 | 0 | 0 | 0 |
| FIBEN | 159 | 31 | 111 | 2 | 0 | 5 | 7 | 3 |

BEAVER's empty-table misses are in neutron (106) and nova (5). All 117 no-key-like misses are in dw (44 from nulls, 73 from duplicates).

**Where false joins enter the final graph (auto-accepted / LLM-accepted):**

| Benchmark | Named | Obfuscated |
|---|---|---|
| BIRD | 15 / 4 | 28 / 14 |
| Spider | 11 / 1 | 34 / 3 |
| BEAVER | 235 / 198 | 362 / 216 |
| KaggleDBQA | 5 / 0 | 6 / 0 |
| FIBEN | 5 / 14 | 23 / 8 |
| **Total** | **271 / 217** | **453 / 241** |

**Abstention:** 44 abstained pairs across named and obfuscated runs, 43 false and 1 true. So abstentions are almost always right, but they stop 43 false joins while 1,182 get through, and 724 of those 1,182 never reached the LLM.

**Ceilings of the candidate fixes:**

| Fix | Ceiling | Verdict |
|---|---|---|
| Route thin evidence to the LLM (source distinct < 5, or `d` None) | Named: 32 of 273 auto-accepted false records, and 15 true ones exposed to the LLM. Obfuscated: 49 of 453, 12 true | About a tenth of false auto-accepts. Cheap but small |
| Near-key targets (uniqueness >= .95, nulls allowed) | Recovers 19 gold FKs on clean data (18 in dw) for 165 extra targets on BIRD and BEAVER | Small on clean data. Worth it only if the sweep shows the null cliff matters |
| Name-based candidates for empty tables | FIBEN: 7 of 111. Neutron: 105 of 106, but the loose rule adds 21,766 false pairs | **Weak on FIBEN, contrary to what I said earlier.** FIBEN's are subclass links with unrelated names. Neutron needs a much tighter rule |

Obfuscated name figures were discarded: `col_NNN` names match each other by construction.

## 3. Plan

### Step 1: abstention and attribution report (no LLM, about an hour)
- Move the gold loading in `evals/grade_joins_all.py` into a `load_gold(dataset)` function. Behaviour unchanged; re-check that it still reproduces .892 / .813 / .633.
- Add `evals/join_attribution.py` from the two scratch scripts. It prints the section 2 tables per dataset.
- This already answers the "does abstaining stop false joins" half on clean data.
- Check how many gold SQL questions touch an empty table. If almost none, the empty-table joins matter for join F1 but not for retrieval, which sets the priority of Fix D.

### Step 2: sparsity sweep harness (no LLM)
`evals/sparsity_sweep.py`. Follow the scratch-copy approach of `perturb_values.py` and `hide_tables.py`: nothing lands in `out/`.

For each point: copy the schema into a temp DuckDB, apply the change, then call `profile_database` (takes any connection), `primary_keys.score_database`, and `foreign_keys.score_database`. Grade `graphs.direct` and `graphs.stats`. Append one line to `out/eval/sparsity/sweep.jsonl`.

Gold chooses which columns are changed, but the pipeline never sees it. Row choice is `hash(rowid, seed)`, so it's reproducible.

| Mode | Change | Rates | Prediction from the code |
|---|---|---|---|
| `fk_null` | Null a share of rows in gold FK source columns | .1 .3 .5 .7 .9 .95 | Nearly flat until few distinct values remain |
| `pk_null` | Null a share of rows in gold target columns | .001 .01 .1 | Recall on those FKs falls to 0 at the first null |
| `parent_delete` | Delete a share of rows from gold parent tables | .1 .2 .3 .5 .7 | Into the band past 20% orphans, gated past 25% |
| `row_sample` | Keep a share of rows in every table, independently | .9 .7 .5 .3 .1 | Mixed: the realistic partial-extract case |

- **Datasets:** BIRD, Spider and FIBEN, named and obfuscated. Add BEAVER nova and neutron as secondary. Skip dw (no declared FKs) and KaggleDBQA (6 FKs).
- **Seeds:** 3. Time one point on BIRD first; if it's slow, drop the largest databases or use 1 seed for the full grid.
- **Metrics:** recall on gold FKs found at rate 0, overall precision / recall / F1, key recovery (Stage 2a), and the counts of accepted, band and gated.
- **FK direction:** BIRD and Spider have it from `labels.gold`. FIBEN's gold is unordered, so take the non-key end as source; for key-to-key links use the table with fewer rows.
- **Check:** one test on a tiny in-memory DuckDB confirming each mode changes what it should, and that rate 0 reproduces the baseline join F1.

### Step 3: containment-threshold sweep (no LLM, minutes)
Rescore stored profiles with `--containment-min` from .5 to .95 on clean data and on the `parent_delete` points. Profiles only store pairs at .5 and above, so going lower needs reprofiling; skip that unless the curve is still rising at .5.

### Step 4: abstention under sparsity (LLM, small)
Run Stage 3 with `--no-describe` on BIRD and FIBEN, named and obfuscated, at two rates per mode (`fk_null` and `row_sample`), 1 seed. Run it inside a scratch root as `hide_tables.py` does.

Measure whether abstention rises with sparsity and whether abstained records stay false. Compare `graphs.stats`, `graphs.llm` and a variant counting abstained as accepted. Check the call count before starting.

### Step 5: fixes, each gated on evidence
Each is a default-off flag, compared on clean data and the sweep. Keep one only if it helps sparse data without lowering clean F1 by more than .01.

| Order | Fix | Change | Run it if |
|---|---|---|---|
| A | Non-null rho | `foreign_keys.py:249`: `(rows - nulls) / distinct` | Always test: one line |
| B | Null-tolerant targets | New `key_like` and `hard_reject` option: uniqueness among non-null >= .95, using the existing x0.7 null penalty | The `pk_null` cliff is confirmed |
| C | Thin-evidence routing | Cap at 69 when `source_distinct` is below a threshold or `d` is None, same as `cap_low_distribution`; needs `source_distinct` on the record | Sweep shows false joins growing as evidence thins |
| D | Empty-table candidates | Name-only records, band at most, flagged `no_values` for Stage 3. Tight rule: source name stem equals the singularized target table, target is its key | Step 1 shows empty tables matter for retrieval |
| E | Sample-size-aware overlap | Discount `v` when few values back it | Only if C isn't enough |

FIBEN's empty-table joins need something other than names, such as one LLM call per empty table proposing a parent. That's new scope and stays out of this plan unless Step 1 shows it matters.

Add a unit test per fix in `tests/test_foreign_keys.py` or `tests/test_profile_distribution.py`.

### Step 6: report
- Retrieval: run Stage 4 and `score_joingr.py` (walk) only for the clean baseline, one mid-sparsity point per mode on FIBEN and BIRD, and any kept fix.
- Write `docs/rq3_results.md`: degradation curves per mode, the abstention result, the attribution tables, and fixes kept and dropped.
- Update `docs/foreign_keys.md` and `docs/stage2b_summary.md` if a default changes.

## 4. What this can and can't show

- The main false-join source is auto-accepted edges (61% overall, 74% outside BEAVER), which abstention can't touch. The likely honest answer is "abstaining is accurate but rare, so it stops few false joins".
- BEAVER's failure is mostly empty tables (neutron), non-key joins (dw) and false joins outside the sparsity mechanisms. This plan won't fix most of it.
- Injected nulls are random. Real sparsity isn't, so `row_sample` and FIBEN's and neutron's real empty tables should be reported as the natural cases.
