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

**Layout.** Scoring expects these folders side by side: `quartile/` (this code), `decode-join/` (JoinGR harness), `JoinGR-E656/` (JoinGR, unchanged) and `data/decode-join-duckdb/` (benchmarks).

**Dependencies.** Python 3.11 or later:

```bash
pip install -r requirements.txt
```

**Data.** Download the DuckDB benchmark files (about 20 GB). `src/duckdb_driver.py` reads this folder first, or `$DECODE_JOIN_DUCKDB_DIR`, and otherwise downloads from Hugging Face.

```bash
hf download SuhxsReddy/decode-join-duckdb --repo-type dataset --local-dir ../data/decode-join-duckdb
```

**LLM.** Stages 3 and 4 use `glm-5-3` on ASU's Voyager gateway. Put `VOYAGER_API_KEY=...` in `.env`. Replies are cached in `out/llm_cache/`. Stages 1–2b need no LLM.

**Run one dataset.** Use `spider`, `bird`, `beaver`, `kaggledbqa` or `fiben`, with `_stripped` or `_obfuscated` for the other settings. Add `--no-names` to Stage 2a for obfuscated datasets.

```bash
python src/profile_columns.py --dataset bird --force
python src/primary_keys.py --dataset bird --run ddb
python src/foreign_keys.py --dataset bird --keys-run ddb --run ddb
python src/describe_columns.py --dataset bird --joins-run ddb --run ddb_low --reasoning-effort low --workers 8
python src/export_joingr.py --dataset bird --stage3-run ddb_low --run ddb_full --reasoning-effort low --workers 8
```

**Repair for truncated data (optional).** Tables left with no join get candidates from shared values:

```bash
python src/profile_columns.py --dataset beaver --overlaps
python src/foreign_keys.py --dataset beaver --keys-run ddb --run repair2 --repair-isolated 2
```

**Evaluate.**

```bash
python evals/grade_joins_all.py --dataset bird --run ddb_low
```

```bash
cd ../decode-join && python ../quartile/evals/score_joingr.py pipeline bird ddb_full
```

**Tests.**

```bash
python -m pytest tests -q
```

## Where things are

JoinGR: https://anonymous.4open.science/r/JoinGR-E656
CoreT: https://github.com/UKPLab/emnlp2026-core-t
BEAVER: Code: https://github.com/beaverbench/beaver , Dataset: https://huggingface.co/collections/beaverbench/beaver-dataset
Spider: https://yale-lily.github.io/spider
BIRD: https://bird-bench.github.io/
KaggleDBQA: https://www.microsoft.com/en-us/research/publication/kaggledbqa-realistic-evaluation-of-text-to-sql-parsers/
FIBEN: https://github.com/IBM/fiben-benchmark
