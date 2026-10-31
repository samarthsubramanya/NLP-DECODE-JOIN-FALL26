# Stage 3: LLM join adjudication and column descriptions — summary and handover

This covers what Stage 3 does, how to run it, and what is still open. For the earlier stages see [stage1_stage2a_summary.md](stage1_stage2a_summary.md) and [stage2b_summary.md](stage2b_summary.md) / [foreign_keys.md](foreign_keys.md).

---

## 1. What problem this part solves

Stage 3 is the only stage that uses an LLM. It has two jobs, in this order:

1. **Resolve the ambiguous joins.** Stage 2b scores every candidate join and labels the weakest accepted ones, those within δ of the cutoff, as `band`. Statistics alone can't settle them. The LLM judges each band record: **accept**, **reject** or **abstain**. It can only judge the record it is given, never propose a join.
2. **Describe every column.** With the final join graph in hand, the LLM writes a readable name and a one-line description for every table and column. It is told not to guess, and marks each column:

| Status | Meaning |
|---|---|
| `resolved` | The evidence clearly supports the meaning |
| `tentative` | Plausible, but another reading fits too |
| `unresolved` | Not enough evidence; name and description are left empty |

The two label sets answer different questions. accept / reject / abstain decides **whether a proposed join is real**. resolved / tentative / unresolved says **how sure a written meaning is**. Descriptions are written after the graph is final and never change it.

---

## 2. What has been added

One script, `src/describe_columns.py`. The Stage 2b output in `out/joins/` is read, never overwritten. Grading lives in `evals/` (`grade_bird.py`, `grade_beaver.py`, `grade_fiben.py`, `hide_tables.py`; §5).

```
out/profiles/<dataset>/<db_id>.json             (Stage 1)
out/keys/<dataset>/<keys_run>/<db_id>.json      (Stage 2a; keys_run is taken from the joins file)
out/joins/<dataset>/<joins-run>/<db_id>.json    (Stage 2b: candidates, graphs.direct, graphs.stats)
        │
        ▼
src/describe_columns.py    Stage 3
   step 1  adjudicate band records  ──►  out/descriptions/<dataset>/<run>/joins/<db_id>.json
                                         (the Stage 2b file + verdicts + graphs.llm)
   step 2  describe columns         ──►  out/descriptions/<dataset>/<run>/<db_id>.json
                                         out/descriptions/<dataset>/<run>/llm_calls.jsonl
                                         out/llm_cache/<hash>.json
```

### Step 1: adjudication

Follows the proposal's Stage 2 text and the PoC's `poc/adjudicate.py`. Two modes (`--adjudication`):

| Mode | Calls | What the LLM judges |
|---|---|---|
| **`grouped`** (default) | one per **source column** | all of that column's band targets together (`c1`, `c2`, …), plus the targets statistics already accepted (`already_accepted`), so it can compare competing targets |
| `single` | one per band record | one record at a time |

Almost every band record (98–100%) is in the band because the fan-out factor ψ pulled a source with several targets below 70, so the targets of one source compete with each other. Grouping lets the model see the competition; on BIRD it gave the same verdicts as `single` (459 of 459) with 177 calls instead of 459.

**What the LLM sees** for a band record `source → target` (`single`; `grouped` sends the source once and each target as a candidate):

| For each side (source and target) | For the record |
|---|---|
| table, column, table row count | `source_is_primary_key`, `target_is_primary_key`, Stage 2a `target_pk_score` |
| column profile: types, null rate, distinct, uniqueness, top and sample values, ranges | containment, coverage, cardinality |
| the table's other column names | the join score, its parts, weights, penalties and fan-out |
| 3 sample rows of the table | the acceptance cutoff and the band `[accept, accept + δ)` |

**Subtype links.** Since 2026-10-31 Stage 2b's G8 lets a sparse integer primary key be a join source (a subclass table reusing its parent's key; see [foreign_keys.md](foreign_keys.md) §4). Without guidance the model rejected these: full containment with low coverage from one shared ID pool looked like coincidence. Both prompts now say that when the source is its own table's key, a real join is usually a subtype or extension link, a shared ID pool is expected, and the question is whether the source entity can be a kind of, or a one-to-one extension of, the target. The examples in the prompt are generic (employee / person), not taken from any benchmark. The paragraph only matters when `source_is_primary_key` is true, so BIRD verdicts were unchanged (identical with names; obfuscated within run-to-run noise).

**What it returns** (constrained by a JSON schema): `{"decision": "accept" | "reject" | "abstain", "reason": "<one sentence>"}`, one per candidate id when grouped. An unreadable reply counts as `abstain`.

**What is written**, on each band record in the copied joins file:

- `decision` becomes `llm_accept`, `llm_reject` or `llm_abstain` (the names Stage 2b's doc reserves);
- `llm` = `{decision, reason, reasoning_effort, model, adjudication}` (+ `group_size` when grouped), and `source_is_pk`;
- `graphs.llm` = accepted + `llm_accept`, built with Stage 2b's own `assemble` (greedy by score, one edge per pair, at most `max_targets` per source).

Rejected and abstained records stay in the file with their reason but are not in `graphs.llm`, so they are never exported. If the input already holds `llm_*` verdicts, they are judged again, so a model or prompt change takes effect.

| Graph | Contains | Written by |
|---|---|---|
| `graphs.direct` | accepted | Stage 2b |
| `graphs.stats` | accepted + band | Stage 2b (RQ1: statistics only) |
| **`graphs.llm`** | accepted + `llm_accept` | **Stage 3** (the full system) |

### Step 2: descriptions (one call per table)

**What the LLM sees:**

| Per column | Per table |
|---|---|
| name, declared type, type inferred from values, Stage 1 role guess | table name and row count |
| null rate, distinct count, uniqueness | 3 sample rows |
| top 5 values with counts, 5 sample values (long strings clipped to 80 chars) | names of the other tables in the database |
| numeric min / quartiles / max / mean, date range | |
| `is_primary_key` from Stage 2a | |
| `joins`: its edges in `graphs.llm`, each with direction (`references` / `referenced_by`), cardinality and `accepted_by` (`statistics` or `llm`) | |
| `lookup`: when the column references a table of ≤ 100 rows, that table's label column and key → label pairs from its sample rows | |

Example from BIRD `financial`: `account.district_id` references `district.district_id`, and gets the lookup pairs `35 → "Jablonec n. Nisou"`, `42 → "Havlickuv Brod"` from `district.A2`.

**What it returns** is constrained by a JSON schema built from the table's **exact column names**, so no column can be dropped or renamed:

```json
{
  "table": {"name": "...", "purpose": "..."},
  "columns": {
    "<exact column>": {"role": "identifier | foreign_key | date | numeric | category | code | free_text | boolean",
                       "name": "...", "description": "...", "status": "resolved | tentative | unresolved"}
  }
}
```

A column missing from the reply, or with an invalid status, is stored as `unresolved`, and so is a column marked `resolved` or `tentative` with an empty description. A table whose call failed is marked `"ok": false` and listed in the console output.

### Information boundary

Same as Stages 1–2b. Stage 3 reads only the profiles, the keys and the joins. It never opens a database or reads declared keys, the `*_metadata` schemas, `schema_mapping.csv`, gold SQL, or the descriptions that ship with a benchmark (e.g. BIRD's `database_description/`). Those are evaluation labels.

---

## 3. The LLM: ASU Voyager

The model runs on ASU Research Computing's Voyager gateway, which is OpenAI-compatible, free, and keeps prompts on RC hardware. Both steps use the same model and settings.

| Setting | Value | Why |
|---|---|---|
| Endpoint | `https://openai.rc.asu.edu/v1/chat/completions` | the gateway; `/v1` is required off campus |
| Model | `glm-5-3` | the largest general model on the gateway; change it with `--model` |
| Reasoning effort | `medium` (code default); **`low` in every reported run** | at `medium`, glm-5-3 sometimes thinks until `max_tokens` and returns no content (40% of obfuscated description calls); `low` had no such failures |
| Fallback | `none` | when a reply comes back empty, the same request is retried once without reasoning; the effort used is recorded on the result |
| `max_tokens` | 32000 | thinking uses the output budget; too small returns empty content |
| Temperature | 0 | reproducible output |
| Parallel calls | 4 (code default); 8 in the reported runs | the gateway docs suggest 4–8 per model |
| Retries | 429: wait 60 s; 502/503/504 and network errors: 5, 10, 20, 40 s | per the gateway's limits page |
| Timeout | 30 min | queue wait (up to 5 min) counts toward it |

Other models named in the gateway docs: `qwen36-27b`, `gemma4-31b-it`, `olmo3-32b-instruct`, `kimi-k2-7-code`, `devstral2-123b`. **Check `GET /v1/models` on first use** to confirm `glm-5-3` is enabled for your key and has `supports_response_schema`.

Every reply is cached by a hash of the full request (model, settings, prompt), so reruns are free and identical. Changing the model or any setting makes a new cache entry. `--repeat N` (N > 0) asks again instead of replaying the cache, and caches that sample as repeat N; it is how run-to-run variance is measured.

**Temperature 0 is not deterministic on the gateway:** across 3 repeats of grouped adjudication, verdicts agreed on 100% of BIRD's 459 band records with names, 92.8% of 97 obfuscated, 96.2% of FIBEN's 26. Join F1 moved by at most 0.002, so single runs are reported.

**Number of calls:** grouped, one per band source column plus one per table. On BIRD dev with names: 180 adjudication calls for 467 band records (177 for 459 before the G8 change) and 75 tables.

**On sending Parquet:** the model only reads text, so data has to go in as text. Parquet is fine for storing data on our side but doesn't reduce prompt size. If token use becomes a problem, sending the sample rows as CSV instead of JSON is the cheapest saving.

---

## 4. How to run it

1. Create an API key in the Voyager portal (**Keys** tab; the portal itself needs the ASU network or VPN).
2. Put it in the repo's `.env` (already gitignored) or the environment:
   ```
   VOYAGER_API_KEY=...
   ```
3. Run Stage 2a and Stage 2b first if their outputs are missing, then:

```bash
PY=/opt/anaconda3/envs/nlp/bin/python
export HF_HUB_OFFLINE=1

$PY src/describe_columns.py --dataset bird --databases financial --run try            # one database
$PY src/describe_columns.py --dataset bird                                            # all of BIRD dev
$PY src/describe_columns.py --dataset bird --names-only --run names_only              # RQ2 ablation
$PY src/describe_columns.py --dataset bird --joins-run frozen_no_names --run no_names # obfuscated simulation
$PY src/describe_columns.py --dataset spider --model qwen36-27b --run qwen            # another model
```

| Flag | Default | Meaning |
|---|---|---|
| `--run` | `default` | output sub-folder; name it after what you changed |
| `--joins-run` | `default` | which Stage 2b run to read; its `keys_run` picks the Stage 2a keys |
| `--model` | `glm-5-3` | any chat model id from `/v1/models` |
| `--reasoning-effort` | `medium` | `none`, `low`, `medium`, `high` |
| `--workers` | 4 | parallel calls |
| `--adjudication` | `grouped` | `grouped` (one call per source column) or `single` (one per band record) |
| `--no-describe` | off | step 1 only: judge the band and write `graphs.llm` |
| `--repeat` | 0 | N > 0 bypasses the reply cache (variance runs) |
| `--names-only` | off | descriptions without values, sample rows or lookup labels; adjudication is unchanged |

Console line per database, e.g.:
`financial: band 16 -> {'accept': .., 'reject': .., 'abstain': ..}, 7 direct + LLM = .. edges; describe {...}`

---

## 5. Results

All runs: glm-5-3, `low` reasoning, grouped adjudication unless noted. Joins are graded as undirected column pairs against declared foreign keys. Outputs are under `out/descriptions/<dataset>/<run>/` with a `grade.json`.

### Settings

| Setting | What the pipeline sees | Where |
|---|---|---|
| named (stripped) | real table and column names and values; descriptions and declared keys withheld (the pipeline never reads them) | `bird`, `fiben`, `beaver` |
| obfuscated | columns renamed `col_NNN` by position, table names kept: the team's `bird_dev_obfuscated.duckdb` | `bird_obfuscated` |
| obfuscated, tables hidden too | as above, tables renamed `t01`, `t02`, … (`evals/hide_tables.py`) | `bird_obfuscated/hidden_tables_low` |

### Joins (final graph `graphs.llm`)

| Dataset | Gold | Edges | P | R | F1 | Notes |
|---|---|---|---|---|---|---|
| BIRD, named | 104 FKs | 166 | .596 | .952 | **.733** | statistics only (`direct`): .659 |
| BIRD, obfuscated | 104 FKs | 595 | .161 | .923 | **.275** | without names Stage 2b accepts 586 edges outright; the LLM only sees the band |
| FIBEN | 159 FKs | 57 | .579 | .208 | **.306** | 111 FKs touch an empty table; on the 48 with data both sides, recall is 33/48 |
| BEAVER nova | 25 FKs | 146 | .110 | .640 | .187 | |
| BEAVER neutron | 163 FKs | 108 | .398 | .264 | .317 | |
| BEAVER dw | none declared | 445 | – | – | – | 23% recall of joins used in gold SQL |

- **The LLM's verdicts on BIRD named are near perfect:** 17 of 17 real foreign keys in the band accepted, 447 of 450 others rejected. Its effect is limited by what reaches it: precision is lost on edges scored ≥ 70, which it never reviews.
- **FIBEN**: 135 of its 159 FKs are primary key → primary key subclass links. Strict G8 blocked all of them (F1 .196); the G8 density rule plus the subtype prompt bring it to .306. Of the 24 non-declared edges, 11 are implied by chaining declared links (child → grandparent).
- `grade_fiben.py` reads FIBEN's FKs from `data/fiben/FIBEN.sql` (IBM/fiben-benchmark); the DuckDB file has none, because the importer skips the `ALTER TABLE` statements.

### Descriptions (BIRD, against `database_description/`)

| Run | resolved / tentative / unresolved | cosine to own reference | to another column's (chance) | own reference ranked first |
|---|---|---|---|---|
| obfuscated (`single`) | 568 / 189 / 41 | .745 | .635 | 52% |
| obfuscated, tables hidden | 455 / 244 / 99 | .670 | .582 | 47% |

Cosine uses `qwen3-embedding-8b` on Voyager. The gateway occasionally returned another request's vectors, so the grader fetches every batch twice and keeps only matching results.

### Memorization (RQ2)

BIRD is public and the model knows it:

| | Column names recovered exactly from `col_NNN` | Table names recovered |
|---|---|---|
| obfuscated, table names kept | 513 / 755 (68%) | – |
| obfuscated, tables hidden | 277 / 668 (41%) | 36 / 75 |
| FIBEN obfuscated | 0 / 107 non-id columns | – |

- **What counts as recovery:** a column counts only if its name matches exactly, with BIRD's own casing and spacing (`Charter Funding Type`, `scryfallIllustrationId`). Only 52 of the 277 tables-hidden matches are generic names like `id` or `name`. So recovery means recognizing the source dataset, not inferring the meaning.
- **Hiding table names reduces recovery but cannot remove it,** because the values themselves identify public datasets (school records, Magic: The Gathering cards).
- **FIBEN shows no recovery,** so it is the clean benchmark for descriptions.
- **Join verdicts barely depend on names** (the same band verdict in 46 vs 44 of 47 shared records). Hiding table names did let 17 false joins through instead of 6.

### Stripped run and join-recall probe (team, 2026-10-28)

The stripped run (`decode_join_bird_dev_stripped.duckdb`, `out/descriptions/bird_stripped`, made with `--run ""`) is identical to `bird/names`: same profiles, keys and joins, so every LLM request hit the cache and `grade.json` matches apart from its label. `bird/names` is therefore the stripped setting in effect.

The obfuscated run (`bird_dev_obfuscated.duckdb`, columns `col_NNN`) still shows the adjudicator real table names. `evals/probe_join_recall.py` measures what that leaks: a scan of the verdict reasons for hidden column names, an open-recall call per database (table and column names only → joins, against a `tbl_NN` control), and a blind re-adjudication of every band record from the two `table.column` names alone, crossed with the full verdicts. Results go to `<stage3_dir>/probe/`.


---

## 6. Known limits and next steps

1. **Precision without names.** In the obfuscated setting most false joins score ≥ 70 and never reach the LLM. Options: send accepted edges to the grouped judge too, or add a statistic that separates chance overlap (the overlap term gives true and false joins the same ~46 points).
2. **Test the subtype prompt and the G8 density threshold on a second class-hierarchy schema**: both were designed after reading FIBEN's failures.
3. **Descriptions on FIBEN or BEAVER** have no reference text; only BIRD can be scored, and BIRD is memorized.
4. **Lookup labels come from 5 sample rows.** A lookup table's full key → label list would need a database query; 5 pairs are usually enough to show what the codes mean.
5. **Wide tables.** The widest table (`european_football_2.Match`, 115 columns) goes in one call; it has not been cut off at `low` reasoning.
6. **Stage 4** (JoinGR export) is not built.
7. **The 2026-10-31 changes are not committed yet.**

## 7. Changelog

- **2026-10-31** Prompts explain subtype links; band payloads carry `source_is_primary_key`; `--repeat` for variance runs. FIBEN graded against `FIBEN.sql`; BIRD tables-hidden run; 3-repeat variance check.
- **Earlier** Grouped adjudication (default) beside `single`; `--no-describe`; empty-reply fallback without reasoning; empty description → `unresolved`; first Voyager runs on BIRD (named, obfuscated), BEAVER and FIBEN, graded by `evals/grade_*.py`.
