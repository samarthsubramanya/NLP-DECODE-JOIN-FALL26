# Stage 3: column descriptions — summary and handover

This covers what was added for Stage 3, how to run it, and what is still open. For Stages 1 and 2a see [stage1_stage2a_summary.md](stage1_stage2a_summary.md).

---

## 1. What problem this part solves

JoinGR retrieves columns by their text: names and descriptions. Undocumented databases have neither in readable form (think BIRD's `A2`–`A16`). Stage 3 writes, for every table and column, a **readable name** and a **one-line description**, inferred from the data by a frozen LLM.

The LLM is told not to guess. Each column is marked:

| Status | Meaning |
|---|---|
| `resolved` | The evidence clearly supports the meaning |
| `tentative` | Plausible, but another reading fits too |
| `unresolved` | Not enough evidence; name and description are left empty |

---

## 2. What has been added

One new script, `src/describe_columns.py`. Nothing else in the repo was changed.

```
out/profiles/<dataset>/<db_id>.json         (Stage 1)
out/keys/<dataset>/frozen/<db_id>.json      (Stage 2a)
        │
        ▼
src/describe_columns.py    Stage 3   ──►  out/descriptions/<dataset>/<run>/<db_id>.json
                                          out/descriptions/<dataset>/<run>/llm_calls.jsonl
                                          out/llm_cache/<hash>.json
```

### What the model sees (one call per table)

| Per column | Per table |
|---|---|
| name, declared type, type inferred from values, Stage 1 role guess | table name and row count |
| null rate, distinct count, uniqueness | 3 sample rows |
| top 5 values with counts, 5 sample values (long strings clipped to 80 chars) | names of the other tables in the database |
| numeric min / quartiles / max / mean, date range | |
| `is_primary_key` from Stage 2a | |
| `values_found_in`: another table's key column holding ≥ 95% of this column's values (a possible foreign key) | |
| `referenced_by`: the reverse | |

The overlaps come from the Stage 1 containment pairs, restricted to the primary keys Stage 2a picked. They are values only, so small integers can overlap by chance (e.g. `card.card_id` appears to point at `account.account_id`). The prompt tells the model to trust them only when the rest of the evidence agrees. Once Stage 2b exists, its accepted joins should replace these hints.

### What the model returns

The reply is constrained by a JSON schema built from the table's **exact column names**, so no column can be dropped or renamed:

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

Same as Stages 1 and 2a. Stage 3 reads only the profiles and the predicted keys. It never reads declared keys, the `*_metadata` schemas, `schema_mapping.csv`, gold SQL, or the descriptions that ship with a benchmark (e.g. BIRD's `database_description/`). Those are evaluation labels.

---

## 3. The LLM: ASU Voyager

The model runs on ASU Research Computing's Voyager gateway, which is OpenAI-compatible, free, and keeps prompts on RC hardware.

| Setting | Value | Why |
|---|---|---|
| Endpoint | `https://openai.rc.asu.edu/v1/chat/completions` | the gateway; `/v1` is required off campus |
| Model | `glm-5-3` | the largest general model on the gateway; change it with `--model` |
| Reasoning effort | `medium` | inferring a column's meaning from values benefits from some thinking |
| `max_tokens` | 32000 | thinking uses the output budget; too small returns empty content |
| Temperature | 0 | reproducible output |
| Parallel calls | 4 | the gateway docs suggest 4–8 per model |
| Retries | 429: wait 60 s; 502/503/504 and network errors: 5, 10, 20, 40 s | per the gateway's limits page |
| Timeout | 30 min | queue wait (up to 5 min) counts toward it |

Other models named in the gateway docs: `qwen36-27b`, `gemma4-31b-it`, `olmo3-32b-instruct`, `kimi-k2-7-code`, `devstral2-123b`. **Check `GET /v1/models` on first use** to confirm `glm-5-3` is enabled for your key and has `supports_response_schema`.

Every reply is cached by a hash of the full request (model, settings, prompt), so reruns are free and identical. Changing the model or any setting makes a new cache entry.

**On sending Parquet:** the model only reads text, so data has to go in as text. Parquet is fine for storing data on our side but doesn't reduce prompt size. If token use becomes a problem, sending the sample rows as CSV instead of JSON is the cheapest saving.

---

## 4. How to run it

1. Create an API key in the Voyager portal (**Keys** tab; the portal itself needs the ASU network or VPN).
2. Put it in the repo's `.env` (already gitignored) or the environment:
   ```
   VOYAGER_API_KEY=...
   ```
3. Run Stage 2a first if `out/keys/<dataset>/frozen/` is missing, then:

```bash
PY=/opt/anaconda3/envs/nlp/bin/python
export HF_HUB_OFFLINE=1

$PY src/describe_columns.py --dataset bird --databases financial --run try          # one database
$PY src/describe_columns.py --dataset bird                                          # all of BIRD dev
$PY src/describe_columns.py --dataset bird --names-only --run names_only            # RQ2 ablation: no values
$PY src/describe_columns.py --dataset spider --model qwen36-27b --run qwen          # another model
$PY src/describe_columns.py --dataset bird --keys-run no_names --run obf            # keys from another Stage 2a run
```

| Flag | Default | Meaning |
|---|---|---|
| `--run` | `default` | output sub-folder; name it after what you changed |
| `--keys-run` | `frozen` | which Stage 2a run to read primary keys from |
| `--model` | `glm-5-3` | any chat model id from `/v1/models` |
| `--reasoning-effort` | `medium` | `none`, `low`, `medium`, `high` |
| `--workers` | 4 | parallel calls |
| `--names-only` | off | hide values and sample rows (names plus types and keys only) |

---

## 5. Status

- **Tested offline only.** A check with a stubbed LLM on BIRD `financial` confirmed: the containment links (e.g. `account.district_id → district.district_id`), the per-table schema listing every column, the names-only switch, missing columns becoming `unresolved`, and the cache (a second pass makes no calls). The largest request was about 17k characters.
- **Not yet run against Voyager**, since no key was available. The model choice and JSON-schema support are unverified until the first real run.
- **Nothing is committed.**

---

## 6. Known limits and next steps

1. **First real run.** Run on BIRD `financial`, read the output by hand, then all of BIRD dev and Spider dev.
2. **Grading.** Nothing scores the descriptions yet. BIRD ships reference descriptions (`database_description/<table>.csv`); the PoC's `description_reference` metric (token F1 and embedding cosine) can be ported. Also report the share of `resolved` / `tentative` / `unresolved`.
3. **Memorization (RQ2).** BIRD and Spider are public, so the model may know them. Compare normal, `--names-only`, and the obfuscated setting before drawing conclusions.
4. **Replace the overlap hints with Stage 2b joins** once foreign-key discovery exists.
5. **Wide tables.** The widest table (`european_football_2.Match`, 115 columns) goes in one call. If its reply is cut off, split wide tables into chunks of columns.
6. **Token use.** If the per-minute token budget is hit (HTTP 429), send sample rows as CSV.
