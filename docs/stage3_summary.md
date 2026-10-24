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

One script, `src/describe_columns.py`. Nothing outside Stage 3 was changed; in particular the Stage 2b output in `out/joins/` is read, never overwritten.

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

### Step 1: adjudication (one call per band record)

Follows the proposal's Stage 2 text and the PoC's `poc/adjudicate.py`.

**What the LLM sees** for one band record `source → target`:

| For each side (source and target) | For the record |
|---|---|
| table, column, table row count | `target_is_primary_key`, Stage 2a `target_pk_score` |
| column profile: types, null rate, distinct, uniqueness, top and sample values, ranges | containment, coverage, cardinality |
| the table's other column names | the join score, its parts, weights, penalties and fan-out |
| 3 sample rows of the table | the acceptance cutoff and the band `[accept, accept + δ)` |

**What it returns** (constrained by a JSON schema): `{"decision": "accept" | "reject" | "abstain", "reason": "<one sentence>"}`. An unreadable reply counts as `abstain`.

**What is written**, on each band record in the copied joins file:

- `decision` becomes `llm_accept`, `llm_reject` or `llm_abstain` (the names Stage 2b's doc reserves);
- `llm` = `{decision, reason, model}`;
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

A column missing from the reply, or with an invalid status, is stored as `unresolved`. A table whose call failed is marked `"ok": false` and listed in the console output.

### Information boundary

Same as Stages 1–2b. Stage 3 reads only the profiles, the keys and the joins. It never opens a database or reads declared keys, the `*_metadata` schemas, `schema_mapping.csv`, gold SQL, or the descriptions that ship with a benchmark (e.g. BIRD's `database_description/`). Those are evaluation labels.

---

## 3. The LLM: ASU Voyager

The model runs on ASU Research Computing's Voyager gateway, which is OpenAI-compatible, free, and keeps prompts on RC hardware. Both steps use the same model and settings.

| Setting | Value | Why |
|---|---|---|
| Endpoint | `https://openai.rc.asu.edu/v1/chat/completions` | the gateway; `/v1` is required off campus |
| Model | `glm-5-3` | the largest general model on the gateway; change it with `--model` |
| Reasoning effort | `medium` | both judgements benefit from some thinking |
| `max_tokens` | 32000 | thinking uses the output budget; too small returns empty content |
| Temperature | 0 | reproducible output |
| Parallel calls | 4 | the gateway docs suggest 4–8 per model |
| Retries | 429: wait 60 s; 502/503/504 and network errors: 5, 10, 20, 40 s | per the gateway's limits page |
| Timeout | 30 min | queue wait (up to 5 min) counts toward it |

Other models named in the gateway docs: `qwen36-27b`, `gemma4-31b-it`, `olmo3-32b-instruct`, `kimi-k2-7-code`, `devstral2-123b`. **Check `GET /v1/models` on first use** to confirm `glm-5-3` is enabled for your key and has `supports_response_schema`.

Every reply is cached by a hash of the full request (model, settings, prompt), so reruns are free and identical. Changing the model or any setting makes a new cache entry.

**Number of calls:** one per band record plus one per table. On BIRD dev the default Stage 2b run has 459 band records (270 of them in `european_football_2`, 90 in `formula_1`) and 75 tables.

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
| `--names-only` | off | descriptions without values, sample rows or lookup labels; adjudication is unchanged |

Console line per database, e.g.:
`financial: band 16 -> {'accept': .., 'reject': .., 'abstain': ..}, 7 direct + LLM = .. edges; describe {...}`

---

## 5. Status

- **Tested offline only**, with a stubbed LLM on BIRD `financial` (16 band records, 8 tables):
  - every band record gets one call with the payload above, and its verdict is written as `llm_*` with the reason;
  - an unreadable reply becomes `llm_abstain`;
  - `graphs.llm` holds only `accepted` and `llm_accept` edges and contains all of `graphs.direct`; accepting every band record gives exactly `graphs.stats` (23 edges);
  - a rerun re-judges from the cache with no new calls and the same graph;
  - descriptions get the `graphs.llm` joins and lookup pairs; `--names-only` drops values, sample rows and lookups;
  - a missing column becomes `unresolved`, and a failed table is reported;
  - the Stage 2b file in `out/joins/` is left unchanged.
- **Not yet run against Voyager**, since no key was available. The model choice and JSON-schema support are unverified until the first real run.
- **Nothing is committed.**

---

## 6. Known limits and next steps

1. **First real run.** Run on BIRD `financial`, read the verdicts and descriptions by hand, then all of BIRD dev and Spider dev.
2. **Grade the verdicts.** Compare `graphs.direct`, `graphs.stats` and `graphs.llm` against the declared foreign keys (BIRD/Spider `foreign_key_list`, FIBEN constraints): did the LLM's accepts add true joins and its rejects remove false ones? That is RQ1's "how much more from the LLM".
3. **Grade the descriptions.** BIRD ships reference descriptions (`database_description/<table>.csv`); the PoC's `description_reference` metric (token F1 and embedding cosine) can be ported. Also report the share of `resolved` / `tentative` / `unresolved`.
4. **Memorization (RQ2).** BIRD and Spider are public, so the model may know them. Compare normal, `--names-only`, and the obfuscated setting before drawing conclusions. The `--joins-run frozen_no_names` simulation hides names from Stages 2a–2b but not from the LLM; a true obfuscated run needs obfuscated databases.
5. **Lookup labels come from 5 sample rows.** A lookup table's full key → label list would need a database query; 5 pairs are usually enough to show what the codes mean.
6. **Wide tables.** The widest table (`european_football_2.Match`, 115 columns) goes in one call. If its reply is cut off, split wide tables into chunks of columns.
7. **Token use.** If the per-minute token budget is hit (HTTP 429), send sample rows as CSV.
