# DECODE-JOIN PoC: stages 1–3 on BIRD

Raw database in, keys + joins + column descriptions out. Stage 4 (refinement) isn't here; `export.py` only writes the JoinGR graph so retrieval can be measured.

## What it needs

One DuckDB file per database per setting (the team's loader), at the path in `config.toml`:

```
../../datasets/bird_duckdb/<setting>/<db_id>.duckdb      e.g. obfuscated/financial.duckdb
```

- The pipeline only reads these files: tables, columns, types, rows. It never sees gold SQL, declared keys or descriptions.
- `documented` may keep PK/FK constraints (`evaluate.keys_gold = "duckdb"`); until then, keys are graded against BIRD's `dev_tables.json`.
- Obfuscated names are translated for grading through `mapping.json` (original `db.table.col` → setting `db.table.col`). It defaults to the one `baselines/settings.py` wrote; point `evaluate.mapping` at the team's file if theirs differs.
- Reference descriptions for Stage 3 grading come from BIRD's `dev_databases/<db>/database_description/*.csv`.

For the LLM stages, put the key in `decode-join/.env` (git-ignored):

```
DEEPSEEK_API_KEY=...
```

## Run

```bash
.venv/bin/python -m poc.tests.smoke
```

```bash
.venv/bin/python -m poc.pipeline --setting obfuscated --run stats --stages profile keys joins export
```

```bash
.venv/bin/python -m poc.pipeline --setting obfuscated --run full
```

```bash
.venv/bin/python -m poc.evaluate.run --setting obfuscated --run full
```

The smoke test needs no BIRD data and no key. Any config value can be overridden per run, e.g. `--set joins.containment_min=0.9 adjudicate.delta=5`. Name the run after what you changed, so results sit side by side.

## Files

| File | Stage | Does |
|---|---|---|
| `config.toml` | all | every threshold, weight, path, model and metric list |
| `catalog.py` | – | the only code that touches a database |
| `profile.py` | 1 | full-column stats, value-inferred types, patterns, samples |
| `keys.py` | 2a | primary-key scoring |
| `joins.py` | 2b–c | containment, gates, FK score, greedy graph |
| `adjudicate.py` | 2d | LLM on the δ band: accept / reject / abstain |
| `describe.py` | 3 | one LLM call per table: role, name, description, status |
| `evidence.py` | 2d, 3 | what the LLM may see about a column (ablations flip it here) |
| `llm.py` | 2d, 3 | OpenAI-compatible client, temperature 0, disk cache, call log |
| `prompts/` | 2d, 3 | the two prompts, as plain text |
| `export.py` | – | minimal JoinGR graph per variant: `none`, `stats`, `llm`, `llm+desc` |
| `evaluate/metrics.py` | eval | one function per metric, picked by name in the config |
| `evaluate/gold.py` | eval | ground truth, translated into the setting's names |
| `evaluate/retrieval.py` | eval | scores exported graphs with `baselines/` (JoinGR) |
| `evaluate/run.py` | eval | runs the configured metrics, writes `results/poc/...` |

Outputs land in `artifacts/poc/<dataset>/<setting>/<run>/<db>/` (`profiles`, `keys`, `joins`, `descriptions`), plus `llm_calls.jsonl`, `graphs/<variant>/` and a config snapshot. Every join candidate keeps its score parts, gates and decision, so any edge can be traced.

## Changing the evaluation

- **Add a metric:** write a function in `evaluate/metrics.py` with `@metric("name")`, then list it in `[evaluate].metrics`.
- **Change what's reported:** `render()` in `evaluate/run.py`.
- **Change the retrieval rows:** `[evaluate.retrieval]` encoders/methods and `[export].variants`.

## Known behaviour (from the smoke test)

With the proposal's weights, two classic false joins score above the band, so the LLM never sees them:
- small integers (`quantity` 1–5) inside an id range;
- one id range nested in another (PK → PK).

Two optional knobs fix them on the toy data. Both are off by default, so defaults stay faithful to the proposal:
- `joins.coverage_min`: the source must use this share of the target's values;
- `joins.key_sources = "never"`.

Whether they help on BIRD is an experiment, not a default.
