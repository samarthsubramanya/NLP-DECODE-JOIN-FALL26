# DECODE-JOIN

Drft version

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

## Where things are

JoinGR: https://anonymous.4open.science/r/JoinGR-E656
CoreT: https://github.com/UKPLab/emnlp2026-core-t
BEAVER: Code: https://github.com/beaverbench/beaver , Dataset: https://huggingface.co/collections/beaverbench/beaver-dataset
Spider: https://yale-lily.github.io/spider
BIRD: https://bird-bench.github.io/
KaggleDBQA: https://www.microsoft.com/en-us/research/publication/kaggledbqa-realistic-evaluation-of-text-to-sql-parsers/
FIBEN: https://github.com/IBM/fiben-benchmark

## Shared DuckDB

The team database is a single DuckDB file stored in a private Hugging Face repo. Pull it, work on it, push it back, one person at a time. Full explanation and steps: [docs/DUCKDB_SETUP.md](docs/DUCKDB_SETUP.md).
