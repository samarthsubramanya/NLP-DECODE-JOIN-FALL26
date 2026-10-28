# Column profiling (Stage 1) and primary keys (Stage 2a)

> New to this part? Start with the short handover: [stage1_stage2a_summary.md](stage1_stage2a_summary.md).

This page describes exactly what the profiling and primary-key code computes, where the numbers come from, and how they are graded. Update it in the same commit as any change to these files:

| File | Stage | Does |
|---|---|---|
| [src/profile_columns.py](../src/profile_columns.py) | 1 | Full-column statistics, inferred types, value patterns and samples. No LLM. |
| [src/primary_keys.py](../src/primary_keys.py) | 2a | Single-column primary-key scoring from the profiles. No LLM. |
| [evals/eval_primary_keys.py](../evals/eval_primary_keys.py) | eval | Grades the predicted keys against the declared keys. |

The profiling code is ported from [poc/profile.py](../poc/profile.py). The changes are that it reads the shared DuckDB files (one schema per database) and the filtered work list, and it adds a few signals needed for key scoring (marked *new* below).

---

## 1. Inputs, outputs and what the pipeline may see

```
out/filtered/<dataset>/databases.json   which DuckDB file, schema and tables to profile
        │
        ▼
src/profile_columns.py  ──►  out/profiles/<dataset>/<db_id>.json
        │
        ▼
src/primary_keys.py     ──►  out/keys/<dataset>/<run>/<db_id>.json
        │
        ▼
evals/eval_primary_keys.py ──► out/keys/<dataset>/<run>/eval.json, errors.jsonl
```

- `databases.json` is written by `src/filter_for_multitable_datasets.py`. Every table it lists for a database is profiled, not only the gold tables, because retrieval runs over the whole schema.
- Databases are opened read-only through `open_db()` in [src/duckdb_driver.py](../src/duckdb_driver.py).

**Information boundary.** Profiling and scoring read only:
- table names (from `databases.json`),
- column names, types and order (from `information_schema.columns`),
- the rows themselves.

They never read `duckdb_constraints()`, `schema_mapping.csv`, the `*_metadata` schemas, gold SQL or `questions.jsonl`. Those are evaluation labels, and only `eval_primary_keys.py` reads them.

---

## 2. How to run

duckdb is installed only in the `nlp` conda env. `HF_HUB_OFFLINE=1` uses the cached Hugging Face download, so no network is needed.

```bash
PY=/opt/anaconda3/envs/nlp/bin/python

# Stage 1 only
HF_HUB_OFFLINE=1 $PY src/profile_columns.py --dataset bird [--databases financial] [--force]

# Stage 2a only (profiles are made first if missing)
HF_HUB_OFFLINE=1 $PY src/primary_keys.py --dataset bird

# Stage 2a + grading
HF_HUB_OFFLINE=1 $PY evals/eval_primary_keys.py --dataset bird
HF_HUB_OFFLINE=1 $PY evals/eval_primary_keys.py --dataset bird --no-names --run no_names
```

- Profiles are cached in `out/profiles/`. Pass `--force` to `profile_columns.py`, or `--reprofile` to the key scripts, to recompute them after a Stage 1 change.
- Key-scoring flags: `--rules` (§4.6; default `R1 R4 R5`, and `--rules` with no values runs the plain score), `--accept`, `--u-floor`, `--seq-min-density`, `--allow-nulls`, `--no-names`.
- `--run <name>` puts the output in its own folder. Name the run after what you changed so results sit side by side.

---

## 3. Stage 1: what is computed per column

All statistics are exact SQL over the full column, with no row sampling. The settings live in `DEFAULTS` in `profile_columns.py`:

| Setting | Value |
|---|---|
| `top_k` | 10 |
| `sample_values` | 10 |
| `sample_rows` | 5 |
| `seed` | 42 |
| `free_text_min_len` | 30 |
| `code_max_len` | 12 |
| `containment_store_min` | 0.5 |
| `placeholders` | `""`, `N/A`, `NA`, `n/a`, `null`, `NULL`, `None`, `-`, `?`, `unknown`, `Unknown` |

### 3.1 Basic statistics (`profile_column`)

| Field | Definition |
|---|---|
| `name`, `declared_type` | From `information_schema.columns`. |
| `position` | 0-based column order in the table (0 = first column). |
| `rows` | `count(*)` of the table. |
| `nulls`, `null_rate` | `rows - count(col)`, and that divided by `rows`. |
| `placeholder_rate` | Non-null values whose text is in the placeholder list, divided by `rows`. The empty string counts. |
| `zero_rate` *(new)* | Values that cast to the number 0, divided by `rows`. |
| `distinct` | `count(DISTINCT col)`. NULL is not counted. |
| `uniqueness` | `distinct / count(col)`, i.e. over non-null values. Note that the PK score uses a different `u`; see §4. |
| `top_values` | The 10 most frequent non-null values with their counts. Ties are broken by value. |
| `sample_values` | 10 distinct non-null values, ordered by `hash(value ‖ seed)`. The sample is fixed for a given seed. |

"Real" values in the statistics below means non-null values that are not placeholders.

### 3.2 Inferred type family (`family`)

1. The declared type maps to a family:
   - `int`: TINYINT … UBIGINT, INTEGER, BIGINT, HUGEINT
   - `float`: DOUBLE, FLOAT, REAL, DECIMAL, NUMERIC
   - `date`: DATE, TIMESTAMP, TIME
   - `boolean`: BOOL*
   - `text`: anything else
2. If the family is `text`, the real values are test-cast. At least 95% must succeed (`CAST_SHARE`). The first match wins, in this order:
   - `BIGINT` → `int`
   - `DOUBLE` → `float`
   - `TIMESTAMP` → `date`

   SQLite-era databases store many numbers and dates as TEXT, which is why this step exists.
3. A `float` column whose values are all whole numbers becomes `int`.

### 3.3 Type-specific statistics

| Field | When | Definition |
|---|---|---|
| `numeric.min/max/mean/p25/p50/p75` | family int or float | Over the real values cast to DOUBLE (`quantile_cont`). |
| `numeric.density` *(new)* | family int | `distinct / (max − min + 1)`: the share of the [min, max] range in use. 1.0 means a gapless run. |
| `date_range` | family date | `[min, max]` of the values cast to TIMESTAMP. |
| `text.avg_len`, `text.min_len` *(new)*, `text.max_len` | any non-null column | Length of the text form of the real values. `min_len == max_len` means a fixed-length value, such as a code. |
| `text.space_share` | | Share of real values containing a space (a free-text signal). |
| `text.uuid_share` | | Share matching the UUID regex. |
| `text.code_share` | | Share matching `^[A-Z0-9_\-\.]+$` (upper-case or digit codes). |
| `text.composite_share` *(new)* | | Share matching `^[A-Za-z0-9]+([\-_/:|.][A-Za-z0-9]+)+$`, i.e. two or more parts joined by a separator, such as `AB-1023`. |
| `text.date_shape_share` *(new)* | | Share of year ranges and month–year values that don't cast to TIMESTAMP: `2014-2015`, `2014/15`, `07/2019`. Years must be 1800–2099 and months 1–12. |

### 3.4 Role and pattern (`role_of`, `pattern_of`)

`role` is the first rule that matches, in this order:

| Role | Rule |
|---|---|
| `boolean` | ≤ 2 distinct values, all in {0, 1, true, false, t, f, y, n, yes, no}; or family boolean |
| `date`, `float`, `int` | The family of the same name |
| `date` *(new)* | `date_shape_share > 0.9`. This stops `2014-2015` being labelled a code. |
| `free_text` | `avg_len ≥ 30` and `space_share > 0.5` |
| `code` | `avg_len ≤ 12` and `code_share > 0.9` |
| `text` | Anything else |

`pattern` is also the first rule that matches:

| Pattern | Rule |
|---|---|
| `gapless_int` | Family int and `max − min + 1 == distinct` |
| `int` | Any other family int |
| `uuid` | `uuid_share > 0.95` |
| `composite` *(new)* | `composite_share > 0.9`, and the role isn't `date` |
| `code` | role `code` |
| `other` | Anything else |

### 3.5 Per-table fields

| Field | Definition |
|---|---|
| `rows` | `count(*)`. |
| `columns` | The column profiles above, keyed by column name. |
| `sample_rows` | `USING SAMPLE reservoir(5 ROWS) REPEATABLE (42)`. These are whole rows, so each column's values line up with its neighbours' (for the Stage 3 prompts). |

The file-level fields are `db_id`, `dataset`, `duckdb_file`, `duckdb_schema`, `seed` and `containment` (§3.6).

### 3.6 Containment (`containment`) *(new)*

`I(a→b) = |Va ∩ Vb| / |Va|` is the share of column `a`'s distinct values that also appear in column `b`. Stage 2a uses it for rule R1 (§4.6), and the Stage 2 foreign-key step will reuse it.

**Targets `b`** are key-like columns:
- the table has rows,
- no nulls,
- every value distinct (`distinct == rows`),
- family `int` or `text`,
- role not boolean, date, float or free text.

**Sources `a`** are any column (same table or another) with:
- the same family as the target,
- a role outside those four,
- at least one value.

**How values are compared:**
- `int` as BIGINT, so `'007'` equals `7`.
- `text` as **trimmed** text. Spider's `flights.SourceAirport` stores `" APG"` with a leading space.
- NULLs and placeholders are left out.

**Pairs that are skipped:**
- the target has fewer than 0.5 × |Va| distinct values, so containment can't reach 0.5,
- int ranges don't overlap,
- containment comes out below `containment_store_min` = 0.5.

Each stored pair is written as follows:

| Field | Meaning |
|---|---|
| `source`, `target` | `[table, column]` |
| `intersection` | `\|Va ∩ Vb\|` |
| `containment` | `\|Va ∩ Vb\| / \|Va\|` |
| `coverage` | `\|Va ∩ Vb\| / \|Vb\|`: the share of the target's values the source uses |
| `source_distinct`, `target_distinct` | Distinct counts |
| `source_unique` | Whether the source's values are all distinct (a 1:1 link rather than many-to-one) |

Cost: about 30 s for all of BIRD dev or FIBEN, and about 5 s for Spider dev.

### 3.7 Not done yet in Stage 1

- **Percentiles.** Only quartiles are computed, not p5 or p95.
- **Obfuscated setting.** No obfuscated copy of the profiles is written yet. `--no-names` on the key scripts simulates it by ignoring column names.

---

## 4. Stage 2a: primary-key score

The score is adapted from DBAutoDoc. Its settings are in `DEFAULTS` in `primary_keys.py`. Each table is scored on its own; only single-column keys are considered, since composite keys are out of scope.

### 4.1 Hard rejections (checked in this order)

1. **Empty table** (`rows == 0`).
2. **Any null.** On by default (`reject_nulls`); `--allow-nulls` turns it off.
3. **Mostly empty or zero:** `placeholder_rate + zero_rate > 0.5`.
4. **Semantic blacklist.** This rule applies only when names are used, and only if the name does **not** match the PK name pattern. The name is split into lower-case tokens (snake_case, camelCase and spaces), and any token in one of these groups rejects the column:

| Group | Tokens |
|---|---|
| date/time | date, time, datetime, timestamp, year, month, day, hour, minute, created, updated, modified, dob, birthday, birthdate |
| quantity | qty, quantity, count, cnt, total, sum, size, weight, height, length, width, volume, age, percent, pct, ratio, rate |
| financial | price, cost, amount, amt, balance, salary, revenue, fee, payment, tax, budget, income, profit, wage, debt, loan |
| descriptive text | description, desc, comment(s), note(s), remark(s), text, summary, body, title, address, bio, content, message |

### 4.2 Score

```
u     = distinct / rows                      (over all rows, not only non-null)
base  = 50·f(u) + 20·n + 15·d + 15·p
s     = (base × penalties + boost) × phi(position)
```

| Term | Definition |
|---|---|
| `f(u)` | `u` when `u ≥ 0.95`. Below that it falls linearly from 0.95 at u = 0.95 to 0 at `u_floor` = 0.5. |
| `n` | 1 if the name matches `[Ii][Dd]$`, else 0. |
| `d` | Prefix match on the declared type: integer types / UUID → 1.0; VARCHAR / CHAR / STRING → 0.6; TEXT / BLOB / BYTEA / CLOB → 0.2; anything else → 0.3. |
| `p` | Points ÷ 15:<br>• uuid or sequential int (family int with `density ≥ 0.9`) = 15<br>• composite pattern = 5<br>• natural key code = 10, meaning role `code`, **or** a non-integer declared type with `code_share > 0.9`, no spaces and `max_len ≤ 20`<br>• otherwise 0 |

The **penalties** multiply together:

| Penalty | When |
|---|---|
| × 0.7 | The column has nulls. This can't fire while the null hard rejection is on. |
| × 0.5 | Unique (`u == 1`) but the name doesn't match the PK pattern ("atypical name"). |
| × (1 − 0.6·l_FK) | `l_FK = 1` when the name minus its id suffix matches **another** table of the same database (e.g. `account.district_id` → `district`); otherwise 0. Table names are compared lower-case with non-alphanumerics removed, and also in singular form. |

The **surrogate boost** adds +20 when all of these hold:
- the name is `id`, or the name minus its id suffix matches the column's own table (`{table}_id`, `{table}id`, or the singular `customers.customer_id`),
- `u ≥ 0.95`,
- `d ≥ 0.9`.

The **position multiplier** `phi` is 1.0 for position 0, 0.85 for 1, 0.70 for 2, and 0.55 for 3 or later.

**Decision:** a column is accepted at `s ≥ 70`. The table's primary key is the accepted column with the highest score, with ties going to the earlier column; its `evidence` is `"score"`. All accepted columns are kept as `candidate_keys`. Rules R2 and R5 (§4.6) can pick a key when nothing is accepted.

### 4.3 No-names mode (`--no-names`)

This simulates the obfuscated setting:
- The name weight of 20 is spread proportionally over the other three terms: `f(u)` 62.5, `d` 18.75, `p` 18.75.
- The blacklist, atypical-name penalty, `l_FK` and surrogate boost are all switched off.

### 4.4 Choices the paper leaves open

These are our interpretations. Revisit them here if they change.

- The lower anchor of f(u) (0.5).
- p is given in points but multiplied by a weight of 15, so the points are divided by 15.
- "Sequential" means a density of at least 0.9, not strictly gapless.
- The extra natural-code rule for VARCHAR digit strings (BIRD `CDSCode`).
- The blacklist is skipped for id-like names, so `payment_id` and `date_id` survive.
- l_FK is name-based (0 or 1), not value-based.
- The boost is added after the penalties and before phi.
- DuckDB stores SQLite TEXT as VARCHAR, so the TEXT score of 0.2 only ever applies to BLOB columns.

### 4.5 Output (`out/keys/<dataset>/<run>/<db_id>.json`)

The top level has `db_id`, `use_names`, `rules` and `accept`. Each table has:
- `primary_key`: a column name, or null.
- `evidence`: `"score"`, `"only_candidate"` (R2), `"name_only"` (R5), or null.
- `candidate_keys`: a list of column and score.
- `columns`: per column, either `{"rejected": reason}` or `{score, base, parts, multipliers, boost, phi}`. `parts.referenced_by` lists the columns that reference it. This lets you trace every decision.

### 4.6 Optional rules (`--rules`)

Each rule has a part that works from the values alone, so it should carry over to the obfuscated setting. Names can only add to what the values support.

| Rule | What it does | Values / names | Status |
|---|---|---|---|
| **R1 referenced** | Some column (any table) has ≥ 95% of its values inside this one (§3.6). Small integers overlap by chance, so an `int` target needs ≥ 10 distinct values and a `text` target ≥ 2. Effect: **+20** and no atypical-name penalty. | values | **on** |
| **R2 only candidate** | If nothing reaches 70, and exactly one column of a table with ≥ 20 rows passes the hard rejections, is unique and isn't boolean/date/float/free text, it becomes the key (`evidence: only_candidate`). | values | off (no gain on Spider) |
| **R3 value-checked penalties** | The blacklist only rejects when the values agree: date/float family, or a boolean/date/float/free-text role. The FK penalty is dropped when the column is referenced. | values check on a name rule | off (no gain on Spider) |
| **R4 key-like names** | `n = 1` also for names ending in `code`, `key`, `number` or `nbr`, or whose last word is `no` or `num`. The same endings are stripped when comparing a name with table names. The blacklist is skipped for these names. | names | **on** |
| **R5 empty tables** | A table without rows gets the one column named `id` or `{table}<key ending>` (`evidence: name_only`). It's **graded separately and never counted in P/R/F1.** | names | **on**, reported separately |

**Tried and removed:** R3 also used to drop the FK penalty for a table's only unique column. From the values alone, a 1:1 extension table (`hiring.Employee_ID`, a real key) looks the same as a junction table (`Has_Pet.PetID`, no key), and the rule added the Has_Pet false positive without fixing hiring.

---

## 5. Evaluation (`eval_primary_keys.py`)

### Where the gold keys come from

| Dataset | Source |
|---|---|
| BIRD, Spider | `<dataset>_metadata.table_info`: SQLite's `table_xinfo`, where `pk > 0` gives the key column and its order. |
| FIBEN | `duckdb_constraints()` of the FIBEN schema. |
| BEAVER, KaggleDBQA | No declared primary keys, so these can't be graded. |

A table that has no declared key is counted as having none.

### Buckets and metrics

Each table falls into one bucket by its gold key:
- **single:** correct, wrong (another column predicted) or missed (no prediction).
- **composite:** abstained (the desired outcome), predicted a component of the key, or predicted another column.
- **none:** abstained, or predicted something.

The metrics are:

| Metric | Definition |
|---|---|
| precision | correct ÷ tables with a prediction |
| recall | correct ÷ single-key tables |
| candidate recall | The gold key is among any accepted candidate, not only the top one. |
| recall on tables with rows | Recall with empty tables left out. |

`errors.jsonl` has one line per non-correct case, with the gold column's rejection reason or score breakdown. `eval.json` holds the config, overall and per-database numbers, and `gold_pk_lost_because`.

---

## 6. Current results

### Protocol (so the key rules don't overfit)

- **Spider dev is the only tuning set.** Every rule and threshold is decided there.
- **BIRD dev and FIBEN are held out.** They're run once with the frozen config, and nothing is changed after seeing their numbers.
- **Keep a rule only if it raises Spider F1** on top of the rules already kept, without dropping precision below 0.965.
- **Each rule needs a values-only part,** so it can carry over to the stripped and obfuscated settings.

### Spider dev ablation (tuning set, 2026-10-23)

| Run (`--rules`) | P | R | F1 |
|---|---|---|---|
| none (baseline) | 0.965 | 0.833 | 0.894 |
| R1 | 0.969 | 0.955 | 0.962 |
| R2 | 0.966 | 0.864 | 0.912 |
| R3 | 0.965 | 0.833 | 0.894 |
| R4 | 0.970 | 0.985 | 0.977 |
| R5 | 0.965 | 0.833 | 0.894 |
| R1 R3 | 0.970 | 0.970 | 0.970 |
| **R1 R4** | **0.970** | **0.985** | **0.977** |
| R1 R3 R4 / R1 R2 R4 / R1 R4 R5 | 0.970 | 0.985 | 0.977 |

**Frozen: R1 + R4**, plus R5, which is reported on its own line and never enters P/R/F1. R2 and R3 add nothing on top of R1+R4, so they stay off.

### Frozen config, run once on the held-out sets (2026-10-23)

| Dataset | Tables (single / composite / none) | Run | P | R | F1 | R on tables with rows |
|---|---|---|---|---|---|---|
| Spider dev *(tuning)* | 80 (66 / 8 / 6) | baseline | 0.965 | 0.833 | 0.894 | 0.833 |
| | | **frozen** | **0.970** | **0.985** | **0.977** | 0.985 |
| BIRD dev | 75 (66 / 6 / 3) | baseline | 1.000 | 0.924 | 0.961 | 0.924 |
| | | **frozen** | **1.000** | **0.970** | **0.985** | 0.970 |
| BIRD dev, stripped file | 75 (66 / 6 / 3) | frozen | 1.000 | 0.970 | 0.985 | 0.970 |
| FIBEN | 152 (150 / 0 / 2) | baseline | 0.951 | 0.260 | 0.408 | 1.000 (39 tables) |
| | | **frozen** | 0.951 | 0.260 | 0.408 | 1.000 (39 tables) |

On FIBEN, R5's name-only guesses get **111/111** empty tables right. They aren't counted in the table above.

**Settings.** The stripped file (`decode_join_bird_dev_stripped.duckdb`, `--dataset bird_stripped`) has the same tables, columns, column order, types and rows as the documented one; only `bird_metadata` and the declared constraints are gone. The pipeline never reads either, so its profiles and keys are identical to the documented run's, and so are the metrics. Labels always come from the documented file. The obfuscated file (`bird_dev_obfuscated.duckdb`, `--dataset bird_obfuscated`, columns `col_NNN` by position) is graded with `evals/grade_bird.py --mapping position`, because `eval_primary_keys.py` compares real column names.

**Frozen-config misses still open:**
- **BIRD `satscores.cds`.** Nothing references it: it points *to* `schools`. It's capped by the atypical-name penalty.
- **BIRD `formula_1.seasons.year`.** It's blacklisted as date/time. R3 would let it through, but Spider gave no reason to turn R3 on, so it stays off.
- **Spider `hiring.Employee_ID`.** A 1:1 table whose key is also a foreign key, so the FK penalty applies.

**False positives:**
- FIBEN `DISCLOSURE.DISCLOSUREID` and `ELEMENTOFFINANCIALSTATEMENT…ID`, and Spider `orchestra.show.Show_ID`. All three look like real keys that simply aren't declared.
- Spider `network_1.Likes.student_id`: one column of a composite key.

### DBAutoDoc baseline, first run (kept for reference)

`--no-names` ignores names during scoring; it doesn't use the obfuscated setting.

| Dataset | Run | P | R | F1 |
|---|---|---|---|---|
| BIRD dev | baseline | 1.000 | 0.924 | 0.961 |
| | no_names | 1.000 | 1.000 | 1.000 |
| Spider dev | baseline | 0.965 | 0.833 | 0.894 |
| | no_names | 0.957 | 1.000 | 0.978 |
| FIBEN | baseline | 0.951 | 0.260 | 0.408 |
| | no_names | 0.951 | 0.260 | 0.408 |

What the baseline errors showed (this is what led to the rules above):

- **The atypical-name penalty causes most misses.** Unique keys whose names don't end in "id" are capped at about 35. Examples:
  - BIRD: `CDSCode` and `cds` (all of california_schools), `zip_code`
  - Spider: `breed_code`, `Code`, `AirportCode`, `contestant_number`

  Without names, recall is 1.00 on BIRD and Spider.
- **The blacklist** removes `formula_1.seasons.year` and Spider `Sizes.size_code`.
- **l_FK** removes Spider `hiring.Employee_ID`, a key that is also an FK in a 1:1 table.
- **FIBEN:** 111 of 150 keyed tables have no rows, so data-driven scoring can't judge them. All 39 tables that have rows are correct.
- **The "false positives" are undeclared keys:** FIBEN `DISCLOSURE.DISCLOSUREID` and `ELEMENTOFFINANCIALSTATEMENT…ID`, and Spider `orchestra.show.Show_ID`. Precision is understated by these.
- **Composite keys:** 13 of 14 tables abstain correctly. The one exception is Spider `network_1.Likes`, where `student_id` was predicted.

### Open experiments
- Stripped and obfuscated BIRD: done with the frozen config, no retuning (stripped above; obfuscated in `out/descriptions/bird_obfuscated/obfuscated_low/grade.json`). Spider and FIBEN have no stripped or obfuscated files yet.
- Spot-check BEAVER's predictions by hand on about 30 tables, since it declares no keys.
- Get `out/filtered/kaggledbqa/` (run the filter with `--preset kaggledbqa`), then profile BEAVER and KaggleDBQA. Neither has gold keys, so they are profiled but not graded.

---

## 7. Changelog

Add a line per change: the date, what changed, and which results it moved.

- **2026-10-23** First version.
  - `profile_columns.py` ported from `poc/profile.py`, with `zero_rate`, `numeric.density`, `composite_share` and the `composite` pattern added.
  - `primary_keys.py` implements the DBAutoDoc-style score (§4).
  - `eval_primary_keys.py` grades against declared keys.
  - Results in §6.
- **2026-10-23** Stage 1 additions and key rules R1–R5.
  - **Stage 1:**
    - `text.min_len` and `text.date_shape_share`, plus a `date` role for year ranges (§3.3–3.4).
    - The `composite` pattern no longer fires on date-shaped values.
    - Containment toward key-like columns, comparing int values as BIGINT and text as trimmed text (§3.6).
  - **Stage 2a:**
    - Rules R1–R5, each behind a `--rules` flag (§4.6).
    - Ablated on Spider dev only. Frozen default: R1, R4, R5.
    - The R3 "only unique column drops the FK penalty" part was removed after it added a false positive.
  - **Grading:** R2 and R5 picks are counted separately, and R5 picks are left out of P/R/F1.
  - **Held-out results (§6):**
    - BIRD recall 0.924 → 0.970, precision still 1.000.
    - Spider (tuning set) F1 0.894 → 0.977.
    - FIBEN unchanged in the main metrics, with R5 getting 111/111 empty tables.
- **2026-10-23** Moved the grader from `src/eval_primary_keys.py` to `evals/eval_primary_keys.py`. It adds `src/` to its import path, so it runs from any directory. No change in behaviour.
- **2026-10-28** BIRD settings as datasets.
  - `filter_for_multitable_datasets.py` presets `bird_stripped` and `bird_obfuscated`. Neither file has `bird_metadata`, so `--questions-db-file` reads `dev.json` from the documented file. The `bird_obfuscated` preset reproduces the earlier hand-made `out/filtered/bird_obfuscated/databases.json` exactly.
  - `eval_primary_keys.py` reads gold keys from the documented file (`labels.documented`), not the run's file, and refuses `*_obfuscated`.
  - Stripped run: profiles, `frozen` keys and `default` joins identical to the documented run, so PK P/R/F1 is unchanged (1.000 / 0.970 / 0.985).
