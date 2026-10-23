# Stage 1 + Stage 2a: summary and handover

This is the short version: what's been built, how to run it, what the results are, and what to do next. For field-by-field details see [column_profiling.md](column_profiling.md).

---

## 1. What problem this part solves

DECODE-JOIN has to work on databases with no documentation: no column descriptions and no declared primary or foreign keys. Before we can find joins (Stage 2b) or write descriptions (Stage 3), we need two things, both learned from the **data itself**:

1. **Stage 1, column profiles.** A statistical fingerprint of every column: how many nulls, how many distinct values, what type the values really are, what they look like, sample values, and which columns' values sit inside which other columns.
2. **Stage 2a, primary keys.** For every table, which single column is its primary key, if any.

Neither stage uses an LLM. Neither stage looks at the declared keys or the gold SQL; those are only used afterwards, to grade the result.

---

## 2. What has been built

Three scripts in `src/`. Each one reads the output of the one before it.

```
out/filtered/<dataset>/databases.json      (already existed: which DBs and tables to process)
        │
        ▼
src/profile_columns.py     Stage 1    ──►  out/profiles/<dataset>/<db_id>.json
        │
        ▼
src/primary_keys.py        Stage 2a   ──►  out/keys/<dataset>/<run>/<db_id>.json
        │
        ▼
evals/eval_primary_keys.py   grading    ──►  out/keys/<dataset>/<run>/eval.json + errors.jsonl
```

### Stage 1: `profile_columns.py`

This is a port of the team's PoC code (`poc/profile.py`) to the real DuckDB files. For every column of every table, it computes from the **full column**, with no sampling:

| What | Examples of fields |
|---|---|
| Completeness | `nulls`, `null_rate`, `placeholder_rate` (fake empties like `''`, `N/A`), `zero_rate` |
| Distinctness | `distinct`, `uniqueness` |
| Real type, read from the values | `family`: int, float, date, boolean, text. A VARCHAR full of numbers counts as int. |
| What the values look like | `role` (code, free text, date, …), `pattern` (gapless int, UUID, code, composite), text lengths, numeric quartiles, date range |
| Examples | `top_values`, `sample_values`, plus 5 `sample_rows` per table (fixed seed, so the samples are reproducible) |
| Cross-column overlap | `containment`: for every column that could be a key, the share of each other column's values found inside it. This is what tells us "column A looks like a foreign key to column B". |

### Stage 2a: `primary_keys.py`

Every column gets a score, based on the DBAutoDoc paper's formula:

- **Hard rejection** for:
  - any nulls,
  - mostly empty or zero values,
  - a name like a date, an amount or a description.
- **Score:** `50·uniqueness + 20·(name looks like an id) + 15·(data type) + 15·(value pattern)`.
- **Adjustments:**
  - penalties for a unique column with an "odd" name, and for a column that looks like a foreign key,
  - a bonus for `id` / `{table}_id`,
  - a weight for column position (earlier is more likely).
- **Accept at ≥ 70.** The best accepted column is the table's primary key.

This is the **DBAutoDoc baseline**. On top of it we wrote five **rules of our own** and tested each one (§4). Three are on by default:

| Rule | Idea | On? |
|---|---|---|
| **R1 referenced** | If another column's values sit inside this column (≥ 95%), this column is probably a key. It gets a bonus. This rule uses **values only**. | ✅ |
| R2 only candidate | If nothing passes, but exactly one column is unique, take it. | ❌ no gain |
| R3 value-checked penalties | Only apply the name blacklist when the values agree. | ❌ no gain |
| **R4 key-like names** | Treat `…code`, `…key`, `…number` like `…id`. | ✅ |
| **R5 empty tables** | A table with no rows gets its key guessed from the name `{table}ID`. **Reported separately**, never mixed into the main scores. | ✅ |

### Grading: `eval_primary_keys.py`

This compares the predictions with the **declared** keys:

| Dataset | Where the declared keys come from |
|---|---|
| BIRD, Spider | `*_metadata.table_info` |
| FIBEN | Constraints in its DuckDB file |
| BEAVER, KaggleDBQA | No declared keys, so they can't be graded |

It reports precision, recall and F1, keeps tables with composite keys and tables without keys in separate buckets, and writes every mistake with its reason to `errors.jsonl`.

---

## 3. How to run it

Only the `nlp` conda env has duckdb installed. The DuckDB files are already cached locally.

```bash
PY=/opt/anaconda3/envs/nlp/bin/python
export HF_HUB_OFFLINE=1

$PY src/profile_columns.py   --dataset bird --force                # Stage 1 (rebuild profiles)
$PY evals/eval_primary_keys.py --dataset bird --run frozen           # Stage 2a + grading: DBAutoDoc + our rules (default)
$PY evals/eval_primary_keys.py --dataset bird --run baseline --rules # Stage 2a + grading: DBAutoDoc baseline only
$PY evals/eval_primary_keys.py --dataset spider --rules R1 R3 --run try_r1_r3   # try a combination
```

- Datasets ready to run: `spider`, `bird`, `fiben`, `beaver` (the last can't be graded).
- Profiling takes about 5 s for Spider and about 30 s each for BIRD and FIBEN.

---

## 4. Experiment: DBAutoDoc baseline vs. DBAutoDoc + our rules

### 4.1 The two systems compared

| System | What it is | How to run it |
|---|---|---|
| **DBAutoDoc baseline** | The primary-key score from the DBAutoDoc paper exactly as described in §2 (uniqueness, id-like name, data type, value pattern, penalties, boost, position), accepting at ≥ 70. No extra rules. | `--rules` with no values |
| **DBAutoDoc + our rules** | The same score plus the two rules we added that earned their place: **R1 referenced** and **R4 key-like names**. R5 also runs, but its guesses on empty tables are reported separately and never counted. | the default |

### 4.2 What the scores measure

Each number is about **tables**: for each table, did the method pick the right primary-key column? "Right" means the key **declared** in the original database. The method never sees it; only the grader uses it.

| Metric | Question it answers | Formula |
|---|---|---|
| **Precision (P)** | When it names a key, how often is it right? | correct ÷ tables where it named a key |
| **Recall (R)** | Of the tables that really have a one-column key, how many did it find? | correct ÷ tables with a declared one-column key |
| **F1** | Both in one number | harmonic mean of P and R |

Tables whose declared key spans several columns are out of scope. They're counted separately; the right answer there is to name nothing.

### 4.3 Tuning set vs. held-out sets

If you design rules while watching a dataset's score, the score rises partly because the rules fit **that dataset's quirks**, not because they're better in general. That's overfitting. To avoid it, the datasets had two different jobs:

- **Tuning set: Spider dev.** The only dataset used to try rules and decide which to keep. Its scores with our rules are therefore **optimistic**.
- **Held-out sets: BIRD dev and FIBEN.** Never used to choose anything. They were run **once**, after the rules were frozen, and the result was reported as-is. They show whether the rules **generalize** to databases with different naming styles and data.

### 4.4 What was done, step by step

1. **Ran the DBAutoDoc baseline** on all three datasets.
2. **Studied its errors.** Most misses were real keys with names like `breed_code`, `CDSCode` or `contestant_number`. The formula halves the score of any unique column whose name doesn't end in "id", so these could never reach 70.
3. **Wrote five candidate rules (R1–R5),** each an on/off switch. Each one needed a part that works from **values alone**, so it can still help when column names are obfuscated.
4. **Tested them on Spider only**: each rule alone, then combinations. A rule was kept only if it raised Spider F1 on top of the rules already kept, without lowering precision.

   | Rules on (Spider) | P | R | F1 | Decision |
   |---|---|---|---|---|
   | none (DBAutoDoc baseline) | 0.965 | 0.833 | 0.894 | — |
   | R1 | 0.969 | 0.955 | 0.962 | keep R1 |
   | R1 + R4 | 0.970 | 0.985 | 0.977 | keep R4 |
   | R1 + R4 + R2, or R1 + R4 + R3 | 0.970 | 0.985 | 0.977 | no gain: R2 and R3 stay off |

5. **Froze the settings** (R1 + R4) as the default. No changes after this point.
6. **Ran once on BIRD and FIBEN** and reported the numbers below.

### 4.5 Results

| Dataset | Tables with a one-column key | DBAutoDoc baseline (P / R / F1) | DBAutoDoc + our rules (P / R / F1) |
|---|---|---|---|
| Spider dev *(tuning set)* | 66 | 0.965 / 0.833 / 0.894 | 0.970 / 0.985 / 0.977 |
| **BIRD dev (held out)** | 66 | 1.000 / 0.924 / 0.961 | **1.000 / 0.970 / 0.985** |
| **FIBEN (held out)** | 150 (only 39 have rows) | 0.951 / 0.260 / 0.408 | 0.951 / 0.260 / 0.408 (unchanged) |

### 4.6 How to read each row

**Spider dev (tuning set)**
- Recall 0.833 → 0.985: the baseline found **55 of 66** keys, our rules find **65 of 66**.
- Precision 0.965 → 0.970. The two "wrong" picks are one column of a two-column key (`network_1.Likes.student_id`) and `orchestra.show.Show_ID`, which looks like a real key the dataset just doesn't declare.
- This shows the rules work where they were designed. On its own it doesn't prove they generalize.

**BIRD dev (held out): the main evidence**
- Recall 0.924 → 0.970: **61 of 66** with the baseline, **64 of 66** with our rules.
- Precision stays **1.000**: every key either system named was correct, so the rules added no wrong picks.
- Rules chosen only on Spider improved an unseen benchmark, so the gain is real rather than fitted to Spider.
- The two misses left:
  - `satscores.cds`: no other column references it.
  - `formula_1.seasons.year`: the word "year" is on the date blacklist.

  We didn't add fixes for them, because that would be tuning on the held-out set.

**FIBEN (held out)**
- The scores don't move, and the low recall (0.26) is **not** a weakness of either system: **111 of its 150 keyed tables are empty**. A method that reads values has nothing to work with.
- On the **39 tables that have rows, both systems get 39/39 right.**
- Precision 0.951 comes from 2 tables with no declared key where the method picked `DISCLOSUREID` and `ELEMENTOFFINANCIALSTATEMENTID`. They look like genuine keys that FIBEN simply doesn't declare.
- R5 guesses a key from the name on empty tables (`{table}ID`) and gets **all 111 right**. That's a separate line, not part of the scores above, because it's a guess from names rather than evidence from the data.

### 4.7 Caveats

- **Spider dev is small:** 66 keyed tables, so one table moves recall by about 1.5 points.
- **BIRD is held out for the rules, but not perfectly unseen.** We looked at the baseline's BIRD errors when we first ran it. After that, nothing was chosen or adjusted using BIRD.
- **Column names were visible in all these runs.** R4 depends on names, so in the obfuscated setting only R1, the values part, can help. That still needs to be measured (next steps).
- **Remaining misses** are all listed with their reason in `out/keys/<dataset>/frozen/errors.jsonl`. The third one besides the two BIRD misses above is Spider `hiring.Employee_ID`, a key that's also a foreign key.

---

## 5. Things to know before changing anything

- **Keep the information boundary.** Stage 1 and Stage 2a may only read table names, column names, types and rows. Don't read `schema_mapping.csv`, `duckdb_constraints()`, `*_metadata` or the gold SQL there; only the evaluation script may.
- **Keep the tuning protocol.** Try new ideas on Spider only, as a new `--rules` flag with its own `--run` name. Run BIRD and FIBEN only after freezing.
- **Every rule needs a values-only part.** In the obfuscated setting column names are meaningless, so a rule that only works from names won't carry over.
- **Update the doc** ([column_profiling.md](column_profiling.md)): the matching section, the results, and a changelog line.
- **Nothing is committed yet.** The new files are the three `src/*.py` scripts and two docs, plus the generated `out/profiles/` and `out/keys/`. The branch also has unresolved merge conflicts in `baselines/*.py` from a teammate's work; those have nothing to do with this.

---

## 6. Next steps (in suggested order)

1. **Stage 2b: foreign keys and joins.**
   - Port the FK logic from `poc/joins.py`. The proposal's weights are overlap 40, name 20, cardinality 15, target-is-key 15, nulls 10; accept at 60, with containment ≥ 75% as a hard gate.
   - The inputs are already in place: containment pairs in the profiles and primary keys in `out/keys/`.
   - Grade against declared FKs: `foreign_key_list` in `bird_metadata` / `spider_metadata`, and FIBEN's constraints.
2. **Stripped and obfuscated settings.**
   - Build the obfuscated copies (column names replaced with opaque ids) and run the **frozen** key rules on them with **no retuning**.
   - Report any drop as a result. Don't fix it by tuning.
3. **The remaining datasets.**
   - BEAVER: run `primary_keys.py --dataset beaver` and hand-check about 30 tables, since it has no declared keys.
   - KaggleDBQA: run the filter first (`filter_for_multitable_datasets.py --preset kaggledbqa`), then profile it.
4. **Stage 1 gaps.**
   - Add the 5th and 95th percentiles; only quartiles exist today.
   - Write unit tests (`tests/test_profile_columns.py`) on a tiny hand-made DuckDB.
5. **Optional key experiments** (on Spider only):
   - the 1:1-table case (`hiring.Employee_ID`),
   - whether R3 helps once names are obfuscated.
6. **Stage 3: column descriptions.** It will use the profiles (`sample_rows`, `top_values`, `role`) and the accepted joins from step 1 as LLM input.
