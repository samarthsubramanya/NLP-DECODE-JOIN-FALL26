# DECODE-JOIN

Training-free recovery of keys, joins and column descriptions from undocumented databases, feeding an unchanged JoinGR. Stages 1–4 and end-to-end retrieval run on all five benchmarks. See `docs/ddb_eval_2026-11-05.md` and `docs/beaver_repair_2026-11-06.md` for results.

## The problem

JoinGR needs a clean schema to build its join graph: readable names, descriptions, declared keys. Real databases often have none of that (think BIRD's `A2`–`A16`), and JoinGR's retrieval quality drops.

## The idea

Put a step in front of JoinGR. DECODE-JOIN takes the raw tables and produces the graph input JoinGR already accepts, so we don't touch JoinGR's code. Nothing is trained, and the LLM is frozen.

1. **Profile** each column from its values, names and neighbours
2. **Find keys and joins** with statistics (uniqueness, containment), and ask the LLM only about borderline cases. It can accept, reject or abstain, but never invent a join.
3. **Describe** each column, marked resolved / tentative / unresolved
4. **Refine and export** once, in JoinGR's format

## What we're testing

Every database runs in three settings: **documented** (the upper bound), **stripped** (no descriptions or keys) and **obfuscated** (column names replaced too).

- **RQ1, structure:** how much of JoinGR's loss comes back from classic key discovery, and how much more from the LLM?
- **RQ2, meaning:** do descriptions written from names plus values beat names alone, once we control for the model having memorized the benchmark?
- **RQ3, sparsity:** how fast does join recovery degrade on sparse columns, and does abstaining stop false joins?

Metrics: column and table recall@5/15, how often the full gold column set is retrieved, and join precision/recall/F1.

## Datasets

BIRD, Spider, BEAVER, KaggleDBQA, FIBEN.

## Phases (8 weeks)

1. **Week 1:** confirm scope, run JoinGR and BEAVER, write the gold-column extractor from gold SQL.
2. **Weeks 2-3:** measure the gap, build statistical key discovery, baselines on all datasets.
3. **Weeks 4-5:** LLM adjudication for ambiguous joins, containment sweep.
4. **Week 6:** column descriptions, memorization probes, sparsity tests.
5. **Week 7:** blind-annotate 200+ columns, error analysis.
6. **Week 8:** report.

Early check: if the documented vs undocumented gap is tiny on Spider, we spend the rest of our effort on obfuscated BIRD/BEAVER.

## Running it

Run everything from this folder. Stages 1–2b need no LLM and no GPU.

### 1. Setup

Expected layout, with this repository as `quartile/`:

```text
code/
  quartile/                  this repository
  data/decode-join-duckdb/   benchmark DuckDB files
  JoinGR-E656/               JoinGR, unchanged (retrieval only)
  datasets/                  raw KaggleDBQA and FIBEN (retrieval only)
```

Python 3.11 or later:

```bash
pip install -r requirements.txt
```

Download the benchmark files (about 20 GB). `src/duckdb_driver.py` reads this folder, or `$DECODE_JOIN_DUCKDB_DIR`, and otherwise downloads each file from Hugging Face when first used.

```bash
hf download SuhxsReddy/decode-join-duckdb --repo-type dataset --local-dir ../data/decode-join-duckdb
```

For Stages 3 and 4, put `VOYAGER_API_KEY=...` in `.env`. They use `glm-5-3` on ASU's Voyager gateway. Replies are cached in `out/llm_cache/`, so reruns are free.

The work lists in `out/filtered/` are committed, so no filtering step is needed. **Don't re-run the filter for KaggleDBQA:** 4 of its 8 databases were added by hand so the retrieval pool matches JoinGR's, and the filter removes them.

### 2. Run the pipeline

Datasets are `bird`, `spider`, `beaver`, `kaggledbqa` and `fiben`, each also with `_stripped` or `_obfuscated`. For obfuscated datasets, add `--no-names` to Stage 2a.

```bash
python src/profile_columns.py --dataset bird --force
python src/primary_keys.py --dataset bird --run ddb
python src/foreign_keys.py --dataset bird --keys-run ddb --run ddb
python src/describe_columns.py --dataset bird --joins-run ddb --run ddb_low --reasoning-effort low --workers 8
python src/export_joingr.py --dataset bird --stage3-run ddb_low --run ddb_full --reasoning-effort low --workers 8
```

Add `--databases <id> ...` to any stage to run only some databases, and `--no-describe` to Stage 3 when only joins matter.

**Optional repair for truncated data.** Tables left with no join get candidates from shared values. It's off unless you pass the flag. Run the first command once, then use the second in place of the normal Stage 2b:

```bash
python src/profile_columns.py --dataset beaver --overlaps
python src/foreign_keys.py --dataset beaver --keys-run ddb --run repair2 --repair-isolated 2
```

### 3. Grade joins

```bash
python evals/grade_joins_all.py --dataset bird --run ddb_low
python evals/join_attribution.py --dataset bird --run ddb_low
```

Grading `beaver` and `kaggledbqa` needs the graphs from the next step.

### 4. Score retrieval

One-time setup per benchmark, shown for `kaggledbqa`. JoinGR ships graphs for `bird`, `spider` and `beaver`, so run `build_graph` only for `kaggledbqa` and `fiben`. The first `index` run downloads the encoders.

```bash
python -m baselines.build_graph --dataset kaggledbqa
python -m baselines.settings --dataset kaggledbqa --setting documented stripped
python -m baselines.index --dataset kaggledbqa --setting documented
python -m baselines.index --dataset kaggledbqa --setting stripped
```

Then score JoinGR alone and the pipeline's graph. This reports dense (`vanilla`), walk (`heuristic`) and `merged`, and saves per-question rankings in `results/`.

```bash
python evals/score_joingr.py alone kaggledbqa stripped
python evals/score_joingr.py pipeline kaggledbqa_stripped ddb_full
python evals/rescued_lost.py kaggledbqa stripped ddb_full
```

### 5. Other experiments

Truncation test (needs the LLM and the retrieval setup):

```bash
evals/run_truncation.sh /tmp/trunc 0.5 0 bird bird_obfuscated
```

Batching (`--batch-columns`, `--batch-candidates`) is off by default. We don't recommend it; see `docs/stage3_summary.md`.

### 6. Tests

```bash
python -m pytest tests -q
```

### Results

Saved scores are in `out/eval/`, and the write-ups are in `docs/`: `ddb_eval_2026-11-05.md` (all benchmarks), `beaver_repair_2026-11-06.md` (repair, merged ranking, truncation test), `rq3_plan.md` (sparsity plan).

## Where things are

JoinGR: https://anonymous.4open.science/r/JoinGR-E656
CoreT: https://github.com/UKPLab/emnlp2026-core-t
BEAVER: Code: https://github.com/beaverbench/beaver , Dataset: https://huggingface.co/collections/beaverbench/beaver-dataset
Spider: https://yale-lily.github.io/spider
BIRD: https://bird-bench.github.io/
KaggleDBQA: https://www.microsoft.com/en-us/research/publication/kaggledbqa-realistic-evaluation-of-text-to-sql-parsers/
FIBEN: https://github.com/IBM/fiben-benchmark
