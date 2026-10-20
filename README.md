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

## Task 1: JoinGR baselines

`baselines/` wraps JoinGR's own search and metric code (nothing in `../JoinGR-E656` is edited) and scores it per setting.

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m baselines.settings --dataset beaver --setting documented no_desc stripped
.venv/bin/python -m baselines.index --dataset beaver --setting documented     # repeat per setting
.venv/bin/python -m baselines.run --dataset beaver --setting documented       # repeat per setting
.venv/bin/python -m baselines.report
```

Datasets: `beaver` (DW), `bird` (dev), `spider` (dev). Graphs and embeddings go to `artifacts/` (not committed), scores to `results/<dataset>/<setting>/<encoder>/<method>.json`, the table to `results/summary.md`.

`no_desc` isn't a proposal setting. It withholds only descriptions so the stripped loss splits into meaning vs structure.

What we found so far:
- Vanilla matches the paper on all three datasets, and the heuristic matches on Spider. Our heuristic beats the paper on Beaver and BIRD, so the paper probably used different heuristic settings. Ask the mentor.
- JoinGR's gold labels keep the SQL's casing, so 28% of Spider gold tables can never match. That's why Spider caps at ~0.75. Results carry both the paper-protocol score and a `summary_fixed` score.
- With gold fixed, Spider dev is ~0.999 PR@15 for dense retrieval: no headroom. BIRD has little (~0.96). Beaver is where the gap lives.
- Heuristic JoinGR returns nothing without join edges, so on stripped graphs dense retrieval is the real floor.

Still to do: trained JoinGR (in-domain and BIRD → Beaver), the obfuscated setting, KaggleDBQA/FIBEN, and column-level metrics once the gold-column extractor lands.

## Where things are

JoinGR: https://anonymous.4open.science/r/JoinGR-E656
CoreT: https://github.com/UKPLab/emnlp2026-core-t
BEAVER: Code: https://github.com/beaverbench/beaver , Dataset: https://huggingface.co/collections/beaverbench/beaver-dataset
Spider: https://yale-lily.github.io/spider
BIRD: https://bird-bench.github.io/
KaggleDBQA: https://www.microsoft.com/en-us/research/publication/kaggledbqa-realistic-evaluation-of-text-to-sql-parsers/
FIBEN: https://github.com/IBM/fiben-benchmark
