# Saved sparse variants (hp)

**Task from the 2026-11-25 group meeting:** create sparsified versions of the benchmarks so a teammate can write a gold question set on them.

**What gets built:** `evals/sparsity/make_variants.py` saves real DuckDB copies of all 5 benchmarks (BIRD, Spider, FIBEN, BEAVER, KaggleDBQA), at 2 levels, in all 3 settings:

| Level | Rows removed | Cells nulled |
|---|---|---|
| medium | 30% | 10% |
| hard | 50% | 30% |

- **The same rows and cells are removed in every setting.** Rows and cells are chosen by row content and column position (seed 0). So the documented, stripped and obfuscated copies hold identical data under different names, and a question written on the documented copy is valid for its two twins. `manifest.json` checks this for every table (`tables_not_aligned` must be 0).
- **The levels come from the sweep.** Medium sits just past the null cliff, where the pipeline starts to fail. Hard is past both cliffs (`docs/sparsity_hp.md`), so it is the "as hard as possible" setting where questions still have rows to answer from.
- **File names are unchanged.** The unchanged pipeline reads a variant with `DECODE_JOIN_DUCKDB_DIR=../sparse-variants/<level>`. Run it in a scratch copy of the repo, as `evals/run_truncation.sh` does.

**Safety:** this adds new files only. The output goes outside the repo (`../sparse-variants/`). `src/`, the original DuckDB files and every branch are unchanged, including Samarth's `feature/sparse_improvements`.

**Relation to Samarth's branch:** his `evals/sparsity_sweep.py` sparsifies in memory to measure, and he adds null-tolerant key variants in `src/`. These variants are the persisted data both his code and the pipeline on `main` can run on. Nothing here depends on his unmerged changes.

## Build

```bash
python evals/sparsity/make_variants.py                     # medium + hard, all benchmarks
python evals/sparsity/make_variants.py --validate-only     # recheck
```

## Sharing

The files are large and stay out of git. To share them, upload `../sparse-variants/<level>/` to the team's Hugging Face dataset, or a Drive folder, once the team confirms the levels.
