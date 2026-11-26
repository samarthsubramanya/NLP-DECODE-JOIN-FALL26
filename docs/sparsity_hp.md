# Sparsity, threshold distance, sparsity-dependent thresholds, detailed ablations and memorization (hp)

Owner: hp. Started 2026-11-16 from the suggestions Samarth relayed from Abhi:

1. Make Spider, BIRD, FIBEN, BEAVER and KaggleDBQA as sparse and hard as possible. No new datasets.
2. Measure the distance from the thresholds.
3. Sparsity-dependent thresholds.
4. Optional memorization probe: about 5 new questions per database over tables no gold question uses, then the plain LLM against our method.
5. Detailed ablations.

**Status:** code and tests are written, but nothing has been run on the benchmarks yet. Results will go in `docs/sparsity_hp_results.md` (generated).

**Safety:** this adds new files only (`evals/sparsity/`, `evals/memorization/`, this doc). `src/`, `poc/`, `baselines/` and `tests/` are unchanged, and so is every existing `out/` run. Outputs go to `out/sparsity/` and `out/memorization/`. To revert, delete those folders and this file.

**Relation to other work:**
- This builds on `docs/rq3_plan.md` (Samarth, 2026-11-06), which planned a sparsity sweep that was never built. Its modes are covered here, plus `cell_null` and `hard`.
- It does not include Samarth's `feature/sparse_improvements` (null_check: null-tolerant keys, G1 off), which is unmerged. Once that merges, rerun the sweep: it is the fix for the null cliff this sweep is expected to show.
- The LLM-level ablation in `docs/ablation_hp.md` (`evals/ablation/`) is unchanged. This study adds the no-LLM Stage 2b ablations, on clean and on sparse data.

## 1. Sparsify (`evals/sparsity/sparsify.py`)

A sparse copy of the dataset's DuckDB file contains only its schemas plus the `*_metadata` schemas. Row and cell choices are a hash of (table, column, rowid, seed), so stripped and obfuscated copies lose the same data. A table always keeps at least one row.

| Mode | What is removed | Gold used? |
|---|---|---|
| `row_sample` | each table keeps (1 − rate) of its rows, independently (a partial extract, like BEAVER dw) | no |
| `cell_null` | each cell is nulled with probability rate, keys included | no |
| `hard` | `row_sample` + `cell_null` at the same rate (the hardest) | no |
| `fk_null` | nulls in gold FK source columns | chooses columns |
| `pk_null` | nulls in gold FK target (key) columns | chooses columns |
| `parent_delete` | gold parent tables lose rows, which makes orphans | chooses tables |

The pipeline never sees gold. Gold only picks which columns or tables to damage.

## 2. Sweep (`evals/sparsity/sweep.py`)

Each point is (dataset, mode, rate, seed). For each point the sweep sparsifies the data and then runs the unchanged pipeline stages in process:
- Stage 1 `profile_database`
- Stage 2a `score_database` (with `--no-names` for obfuscated)
- Stage 2b `score_database`, once under every arm

It then grades two graphs:
- `direct`: heuristics only
- `stats`: band accepted, no LLM

A rate-0 `clean` point is always run first, so every curve has a baseline. Points that are already finished are skipped on a rerun.

For the LLM graph on one sparse point, use `run_llm_point.sh`. It runs in a scratch copy of the repo and shares the LLM cache.

## 3. Distance from thresholds (`evals/sparsity/margins.py`)

For every candidate it records the signed distance to each threshold:
- accept line (60)
- auto line (70)
- G6 τ (0.75)
- QuantileMD cap (0.3)
- Stage 2a target score (70)

Errors are then split into **near** (within 5 points or 0.05, so moving the threshold fixes them) and **far** (they need new evidence). Gold joins removed by a gate are counted per gate.

It also works on finished team runs:

```bash
python evals/sparsity/margins.py --dataset bird_stripped --run ddb
python evals/sparsity/margins.py --dataset bird_stripped --run ddb_low --stage3
```

## 4. Sparsity-dependent thresholds (`evals/sparsity/adaptive.py`)

Each rule reads only the profile and the Stage 2a keys of that database, never gold. No coefficient is fit to a benchmark.

| Signal | Definition |
|---|---|
| `orphan_level` | 25th percentile of (1 − best containment) over sources whose best pair goes into a chosen primary key with a distribution term that isn't flagged. Capped at 0.25 |
| `null_level` | median null share of candidate FK sources |

| Rule | Change |
|---|---|
| `tau` | τ = max(0.5, 0.75 − orphan_level) |
| `orphan` | the orphan penalty starts at 0.2 + orphan_level |
| `null` | the null-term cutoffs move up by null_level |
| `band` | band width = 10 × (1 + 2 × max(orphan, null)). This is the only rule with a free gain (2) |

At level 0 every rule leaves the configuration unchanged, and a test checks this. The report also checks that the clean-data delta is 0.

## 5. Detailed ablations (`evals/sparsity/arms.py`)

Each arm differs from FULL in one component:
- `no_qmd`, `no_dist_cap`
- `no_name`, `no_cardinality`, `no_target_key`, `no_nulls`: a removed term's points are spread over the other terms, so the total stays 100
- `no_orphan_penalty`, `no_fan_out`, `no_g5`, `no_g8_exception`, `no_adaptive_weights`, `tau_0.5`
- `adapt_*`

All of them run on every point, so each component's effect is measured on clean data **and** under sparsity.

## 6. Memorization probe (`evals/memorization/`)

1. `questions.py unused` lists, per database, the tables no gold question uses.
2. `questions.py write` has the LLM draft questions plus DuckDB SQL over those tables only. A draft is kept only if:
   - it parses,
   - it reads only unused tables,
   - it returns rows on the documented file.

   Up to 5 per database are kept, multi-table first, and saved as `"reviewed": false`. **Each question has to be reviewed by hand.**
3. `probe.py` asks the same LLM for the needed tables and columns in three ways:
   - `closed_book`: database name only
   - `plain`: the schema as the setting shows it
   - `ours`: the catalog DECODE-JOIN exported for JoinGR

   It does this on 5 benchmark questions per database and on the new ones. A large gold-minus-new gap for `closed_book` is memorization. Column recall is the sharper measure in the obfuscated setting, because table names are kept there.

## How to run (Mac, repo root, venv active)

```bash
python -m pytest evals/sparsity -q                       # 14 tests, no data
bash evals/sparsity/run_all.sh time                      # one BIRD db, one point: time it first
bash evals/sparsity/run_all.sh sweep                     # full grid (env vars shrink it)
bash evals/sparsity/run_all.sh report                    # -> docs/sparsity_hp_results.md
python evals/memorization/questions.py unused --datasets bird spider fiben beaver kaggledbqa
```

These need `VOYAGER_API_KEY` in `.env`:
- `questions.py write`
- `probe.py`
- `run_llm_point.sh`
- `evals/ablation/run_ablation.sh`

Gold for BEAVER dw and KaggleDBQA comes from their documented JoinGR graphs (`python -m baselines.settings ...`, see the README).

## Known limits

- Injected sparsity is random, but real sparsity isn't. `row_sample` and the natural empty tables (FIBEN, neutron) are the realistic cases.
- `cell_null` and `hard` null key columns too. Because one null makes a column non-key-like in Stage 1, expect a cliff even at rate 0.1. That is a finding, and it is what Samarth's null_check addresses.
- The adaptive band gain (2) is a free choice. Report it as such, or sweep it on Spider only (the repo's tuning set).
- The memorization questions are LLM-authored over the documented schema, so they need human review before they count.
