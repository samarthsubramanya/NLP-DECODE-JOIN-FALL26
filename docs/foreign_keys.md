# Foreign keys and the join graph (Stage 2b)

> Short handover: [stage2b_summary.md](stage2b_summary.md). This page is the full reference: every rule, where it comes from, what the code computes, and what it produced. Update it in the same commit as any change to `src/foreign_keys.py`.

| File | Stage | Does |
|---|---|---|
| [src/foreign_keys.py](../src/foreign_keys.py) | 2b | Scores foreign-key candidates and builds the join graph. No LLM. |
| [tests/test_foreign_keys.py](../tests/test_foreign_keys.py) | test | 13 unit tests on hand-made profiles (no database, no files). |

---

## 1. What it does and where it sits

Stage 2b answers one question per database: *which column pairs look like a foreign key (source column → target key), and how sure are we?* It follows DBAutoDoc's foreign-key scoring, computed on **whole columns** (exact containment, no sampling), and assembles the accepted pairs into the graph that JoinGR walks.

```
filter_for_multitable_datasets.py  → out/filtered/<dataset>/databases.json
profile_columns.py        Stage 1   → out/profiles/<dataset>/<db_id>.json     stats + containment pairs
primary_keys.py           Stage 2a  → out/keys/<dataset>/<run>/<db_id>.json    primary keys, eligible columns, scores
foreign_keys.py           Stage 2b  → out/joins/<dataset>/<run>/<db_id>.json    scored candidates + join graph
(Stage 3, LLM)                        judges the ambiguous "band" records, then writes descriptions
```

**Information boundary.** Stage 2b reads only the Stage 1 profiles and the Stage 2a keys. It never opens a database, and never touches declared keys, `schema_mapping.csv`, the `*_metadata` schemas or gold SQL. Those are evaluation labels; evaluation happens after Stage 3.

**What Stage 2b does not do.** It has no LLM step. The records that need a judgement are *labelled* `band` here; the LLM that accepts, rejects or abstains on them belongs to Stage 3 (it is not built yet).

---

## 2. Where each rule comes from

Tags used in the code and in this page:

| Tag | Source |
|---|---|
| **[Paper]** | DBAutoDoc: Nagarajan and Altman, 2026, *DBAutoDoc: Automated Discovery and Documentation of Undocumented Database Schemas via Statistical Analysis and Iterative LLM Refinement*, arXiv:2603.23050. Section 4.3 and Appendix A.2 (score), A.3 (gates), A.4 (fan-out). |
| **[Report]** | Our project proposal (DECODE-JOIN), "Stage 2: Keys & joins" |
| **[Team]** | The formulas the team wrote down for the PoC config (name-similarity strings, prefilter thresholds) |
| **[Ours]** | An interpretation or addition of ours; always listed in §9 |

Related work the design leans on, as cited in the proposal:

| Work | What we take from it |
|---|---|
| DBAutoDoc (Nagarajan and Altman, 2026) | The score, penalties, gates, fan-out, adaptive weights, acceptance threshold 60 |
| HoPF, Jiang and Naumann, 2020, *Holistic primary key and foreign key detection*, J. Intell. Inf. Syst. 54(3) | Adding accepted foreign keys to the graph greedily, highest score first |
| LLM-FK, Tang et al., 2026, arXiv:2603.07278 | Finding candidates by an inclusion-dependency test over the full columns, which needs no names |
| Kruse et al., 2015, *Scaling out the discovery of inclusion dependencies* | Foreign keys as inclusion dependencies |
| JoinGR, De et al., 2026 | The consumer: an undirected graph whose join edges are foreign keys; cycles are fine |
| CORE-T, Soliman et al., 2026 | Training-free retrieval from containment and uniqueness without gold keys (the comparison system) |

---

## 3. Candidates

A candidate is a **Stage 1 containment pair**: source column `a` → target column `b`, with

`I(a→b) = |Va ∩ Vb| / |Va|`

(Va, Vb are the distinct non-null, non-placeholder values; ints are compared as BIGINT, text as trimmed text; computed exactly over the full columns). See `column_profiling.md` §3.6 for how the pairs are made. What that means for Stage 2b:

- Targets in the pairs are *key-like* columns only: rows present, no nulls, every value distinct, family int or text, role not boolean/date/float/free text.
- Sources are any column of the same family (not boolean/date/float/free text).
- Pairs below containment 0.5 are not stored, and pairs whose target has fewer than half as many distinct values as the source cannot reach it.
- Each pair carries `containment`, `coverage` (the share of the target's values the source uses), `source_distinct`, `target_distinct` and `source_unique` (whether the source is also fully distinct, i.e. a 1:1 link).

Stage 2b then takes the **target's Stage 2a verdict** from `out/keys`: which columns survived the hard rejections, which one is the chosen primary key, and its score.

---

## 4. Gates and prefilters

Checked in this order. The first one that fires rejects the pair as `rejected_gate`, with the reason stored in `reason`.

| # | Rule | Reason text starts with | Tag |
|---|---|---|---|
| 1 | Source and target in the same table, unless `--self-refs` | `same table` | [Ours] |
| 2 | **G8:** the source is its table's chosen primary key and not a sparse integer key (density ≤ `pk_source_max_density`, 0.5) | `G8` | [Paper], [Report]; the exception is [Ours] |
| 3 | **G1:** the target was hard-rejected by Stage 2a (empty table, nulls, mostly empty/zero, or a blacklisted name when names are used). Uniqueness is already guaranteed, because Stage 1 only makes pairs for fully unique targets. The target does *not* have to be the chosen key | `G1` | [Paper] |
| 4 | **G3:** the target column is named `rowguid` (only when names are used) | `G3` | [Paper] |
| 5 | Source role is boolean, free text or float | `prefilter: source role` | [Paper] tier 1 |
| 6 | Source has fewer than 2 distinct values | `prefilter: fewer than 2` | [Ours] |
| 7 | Source average length is over 64 characters | `prefilter: average length` | [Paper] tier 2, [Team] |
| 8 | **G6:** containment is below 0.75 (`--containment-min`) | `G6` | [Paper], [Report] |
| 9 | **G9:** both columns are integers and the source's range `max − min + 1` spans less than `min_range_span` (0.1) of the target's (`--min-range-span`, negative = off) | `G9` | [Ours] |

The paper's G8 is strict: a column that is *both* its table's primary key and a real foreign key cannot be found. That loses 1:1 extension tables (BIRD `frpm.CDSCode → schools.CDSCode`) and subclass tables (FIBEN `CORPORATION.CORPORATIONID → AUTONOMOUSAGENT.AUTONOMOUSAGENTID`; 135 of FIBEN's 159 declared FKs are such key → key links). Simply dropping G8 lets in keys numbered 1..n that overlap another table's 1..n by chance (BIRD: 17 extra accepted edges, 1 real).

What separates the two is how densely the key fills its own range, `density = distinct / (max − min + 1)` (Stage 1's `numeric.density`). On the key → key candidates passing G6:

| | candidates | median source-key density |
|---|---|---|
| BIRD, not declared | 130 | 0.999 |
| BIRD, declared | 3 | 0.0004 |
| FIBEN, declared | 28 | 0.0008 |

So G8 spares an **integer** primary key whose density is at most `pk_source_max_density` (0.5): a sparse key whose scattered values all land in another key rarely does so by chance. That admits 3/3 BIRD and 19/28 FIBEN real links and 13/130 BIRD chance pairs (7 of them `european_football_2` ids that do match, undeclared). Dense real links (FIBEN `PERSON.PERSONID`, density 1.0) are still lost; text keys keep strict G8. `--pk-source-max-density -1` restores the paper's strict G8.

**G9, why the range matters.** Without names, 501 of the 600 edges in BIRD obfuscated's final graph were wrong, nearly all small integer measures (a 0–100 rating, a 1–5 count) sitting inside a table's 1..n ids. Containment cannot tell them apart: any value set in 1..100 is contained by ids 1..11,060. What differs is how much of the target's range the source covers. A real foreign key draws from the whole parent key; a measure stays in a corner. On the edges of that graph, `span = (source max − min + 1) / (target max − min + 1)`:

| | edges | 10th pct | median |
|---|---|---|---|
| declared FK | 99 | 0.93 | 1.00 |
| not declared | 501 | 0.0001 | 0.004 |

The smallest declared span is 0.14 (`pitstops.raceId`: recent races 841–982 of 1–988), so 0.1 loses no BIRD foreign key. Text pairs are not checked (no order to measure).

---

## 5. The score

For every pair that passes the gates, in points out of 100:

`s_FK = 40·v + 20·s + 15·r + 15·k + 10·ν`

| Term | Weight | Definition in the code |
|---|---|---|
| `v` overlap | 40 | The containment `I(a→b)`, exact over the full columns. (The paper uses up to 500 sampled values; we do not sample.) |
| `s` name | 20 | Best of three comparisons between the source column name and the target: the target column `b`, `{b_table}_{b}`, and `{b_table}_id`. Names are lower-cased with only letters and digits kept (the same form Stage 2a uses). `1.0` if equal, `0.8` if one contains the other, else `1 − Levenshtein / longest length`. [Paper] defines the three cases; the three strings are [Team]. |
| `r` cardinality | 15 | `min(ρ, 2) / 2`, with `ρ = rows(source table) / distinct(source column)`. A source that repeats its values (many rows per value) scores high; a unique source scores 0.5. |
| `k` target is a key | 15 | `1` if the target is its table's **chosen primary key** from Stage 2a, otherwise `0`. Eligible targets that were not chosen get `0`. |
| `ν` nulls | 10 | Null share of the **source**: below 30% → `1.0`, below 70% → `0.5`, otherwise `0`. |

### Weight modes

| Mode | When | Weights (overlap / name / cardinality / key / nulls) |
|---|---|---|
| Normal | names used | 40 / 20 / 15 / 15 / 10 |
| No names | the keys run was made with `--no-names` (the obfuscated simulation) | 60 / – / 15 / 15 / 10. The 20 name points go to overlap [Report]. |
| Adaptive | more than 40% of the candidates that passed the gates have `k = 0` | the 15 key points go to overlap (40 → 55) [Paper]. Decided **per database** [Ours]. If both apply, both moves go to overlap. |

Every output file stores the weights actually used, plus `k_zero_share` and whether the adaptive shift fired.

---

## 6. After the score

Applied in this order:

1. **Orphan penalty:** × 0.7 when the orphan rate `1 − v` is above 0.2 [Paper].
2. **Incompatible types:** × 0.5 [Paper]. Inert here, because Stage 1 pairs are always the same family.
3. **G4 row-count ratio:** the paper says confidence is scaled down when the source table has far fewer rows than the target but gives no multiplier. It is a **knob that is off**: `--g4-ratio R --g4-multiplier M` multiplies the score by `M` when `rows(source) < R · rows(target)`.
4. **G5:** each source column keeps its **3 best** targets (ranked by the score so far); the others become `rejected_gate` (`G5: more than 3 targets`).
5. **Fan-out ψ(n):** the kept score is multiplied by ψ, where `n` is the number of targets kept for that source: **1.0** for 1, **0.85** for 2, **0.75** for 3, 0.65 for 4 or more [Paper]. `n` counts targets kept after G5 [Ours, chosen with the team]; the paper's wording is "target tables".

The result is `score`; `score_before_fan_out` is kept as well.

Note that ψ applies the same factor to all kept targets of a source, so a clearly correct pair is demoted together with weak ones whenever its source has several surviving targets (see the worked example). **Decision (team, 2026-10-24): keep ψ exactly as the paper has it.** A pair that drops into the band is not lost; deciding the ambiguous ones is the LLM's job in Stage 3.

---

## 7. Decision, band and graph

### Decision

On the final `score`, with cutoff `accept = 60` and band width `delta = 10`:

| Score | Decision | Meaning |
|---|---|---|
| below 60 | `rejected_score` | Never reaches the LLM |
| 60 to below 70 | `band` | Passes the cutoff but is among the weakest: Stage 3's LLM will accept, reject or abstain |
| 70 and above | `accepted` | Added to the graph directly |

Stage 3 will record its verdicts as `llm_accept`, `llm_reject` or `llm_abstain`. The LLM can only judge band records; it cannot propose a join, and rejected or abstained records are not exported [Report].

### Graph

Built greedily, as in HoPF [Report]: records are sorted by score, highest first, and added one by one; a pair gets **one edge** even if it qualifies in both directions (the graph is undirected, as JoinGR expects), each source gets at most 3 edges, and cycles are kept.

| Graph | Contains | Used for |
|---|---|---|
| `graphs.direct` | accepted records | The base the LLM's accepts are added to |
| `graphs.stats` | accepted + band records | What statistics alone would export (RQ1, "statistical discovery" variant) |
| `graphs.llm` | accepted + `llm_accept` | Written by Stage 3 (the full variant) |

Each edge holds `source`, `target`, `containment`, `coverage`, `cardinality`, `score` and `decision`, the fields the PoC's export, describe and evaluation code already read. The detailed terms are on the matching `candidates` record.

---

## 8. Output file

`out/joins/<dataset>/<run>/<db_id>.json`

| Field | Meaning |
|---|---|
| `db_id`, `use_names`, `keys_run` | Which database; whether names were used (inherited from the keys run); which Stage 2a run it read |
| `weights`, `adaptive`, `k_zero_share` | The weights actually used and why |
| `config` | Every setting of the run |
| `counts` | Records per decision |
| `candidates` | One record per Stage 1 pair (see below), sorted accepted → band → rejected |
| `graphs` | `direct` and `stats` edge lists |

A `candidates` record has: `source`, `target`, `target_is_pk`, **`target_pk_score`** (the Stage 2a score, for the LLM prompt), `containment`, `coverage`, `cardinality` (`one_to_one` or `many_to_one`), `decision`, and, for pairs that were scored, `parts` (the terms above), `name_via`, `penalties`, `score_before_fan_out`, `fan_out`, `score`. Gated pairs carry `reason` instead.

### Worked example (BIRD `financial`)

`card.disp_id → disp.disp_id`:

| | Value |
|---|---|
| containment `v` | 1.0 |
| name `s` | 1.0 (same name) |
| cardinality `r` | 0.5 (`card.disp_id` is unique, so ρ = 1) |
| key `k` | 1.0 (`disp_id` is the chosen primary key; target PK score 125) |
| nulls `ν` | 1.0 |
| base score | 40 + 20 + 7.5 + 15 + 10 = **92.5** |
| penalties | none (containment is 1.0) |
| fan-out | `card.disp_id` has 3 surviving targets → ψ = 0.75 |
| final score | 92.5 × 0.75 = **69.375** → `band` |

---

## 9. Choices the sources leave open

Decided together with the team unless marked otherwise.

| Choice | What we do |
|---|---|
| Which targets count (G1) | Every column Stage 2a did not hard-reject. `k = 1` only for the chosen primary key |
| G4 multiplier | Not specified in the paper: off by default, configurable |
| ψ(n) counts | Targets kept after G5 |
| Same-table pairs | Excluded unless `--self-refs` |
| Name comparison form | Lower-case letters and digits only, as in Stage 2a *(our choice, not asked)* |
| Adaptive-weight scope | One database *(our choice, not asked)* |
| No names and adaptive together | Both moves go to overlap *(our choice, not asked)* |
| Email/URL prefilter (paper tier 2) | Not applied: it needs a full-column share that Stage 1 does not compute; only the average-length rule is used |
| Fan-out ψ | Kept exactly as in the paper, even though it can push a clear winner into the band (decided 2026-10-24) |
| Coverage as an extra signal | Not used. Stage 1 stores `coverage`, but it is not in the paper or the proposal |
| G8 exception | Integer primary keys with density ≤ 0.5 may be sources (§4); threshold chosen on FIBEN and BIRD, checked on Spider dev and BEAVER *(decided 2026-10-31)* |
| G9 range span | Integer pairs need source range ≥ 0.1 × target range (§4); threshold set below the smallest BIRD span (0.14), checked on FIBEN, Spider dev and BEAVER *(2026-11-01)* |

---

## 10. Known limits

- **G8:** a primary key that is also a foreign key is found only when it is a sparse integer key (§4); dense ones (keys numbered 1..n) and text keys are not.
- **Containment is not enough on its own.** Small integer columns overlap by chance with a large id range; G9 removes those that sit in a corner of the range, and the name and cardinality terms and the fan-out penalty handle the rest. A measure whose range happens to match an id range (1–77 counts against ids 1–77) still passes. Expect statistics alone to be noisy, and many borderline records to reach the band.
- **No names:** with the 20 name points moved to overlap, most candidates clear 70 and the band nearly empties (§11).
- **Fan-out** demotes a correct pair whenever its source has several surviving targets (§6). Kept on purpose; the LLM band absorbs it.
- **Empty tables** (e.g. FIBEN) have no values to compare, so they produce no candidates.
- The score depends on Stage 2a's choices: a wrong primary key changes `k` for every pair into that table.

---

## 11. Running it

```bash
# the whole chain on BIRD dev (documented / stripped: they look identical to the pipeline)
python src/filter_for_multitable_datasets.py --preset bird --out out/filtered/bird
python src/profile_columns.py   --dataset bird
python src/primary_keys.py      --dataset bird --run frozen
python src/foreign_keys.py      --dataset bird --run default

# obfuscated simulation: same rules, column names ignored
python src/primary_keys.py      --dataset bird --run frozen_no_names --no-names
python src/foreign_keys.py      --dataset bird --keys-run frozen_no_names --run frozen_no_names

# the stripped and obfuscated files: the setting is the dataset name (bird_stripped, bird_obfuscated)
python src/filter_for_multitable_datasets.py --preset bird_stripped --out out/filtered/bird_stripped
python src/profile_columns.py   --dataset bird_stripped
python src/primary_keys.py      --dataset bird_stripped --run frozen
python src/foreign_keys.py      --dataset bird_stripped --keys-run frozen --run default

python -m pytest tests/test_foreign_keys.py
```

Flags: `--databases`, `--run`, `--keys-run`, `--accept`, `--delta`, `--containment-min`, `--max-targets`, `--self-refs`, `--pk-source-max-density` (negative = strict G8), `--min-range-span` (negative = G9 off), `--g4-ratio` / `--g4-multiplier`, `--reprofile`. The names setting always comes from the keys run.

The first command needs the DuckDB file from the shared Hugging Face repo (`src/duckdb_driver.py` downloads it); set `HF_HUB_OFFLINE=1` afterwards to use the cached copy.

---

## 12. What it produced on BIRD dev (not graded)

Counts only. Gold labels are used for evaluation after Stage 3, never here. 1,502 candidates in total; the adaptive shift fired in no database. Regenerated 2026-11-01 with G9 (before G9: 148 / 467 / 90 / 797 with names, 586 / 106 / 10 / 800 without; strict G8: 145 / 459 / 86 / 812 and 581 / 97 / 7 / 817).

**With names** (`out/joins/bird/default`)

| Database | accepted | band | below 60 | gated out |
|---|---|---|---|---|
| california_schools | 2 | 0 | 1 | 1 |
| card_games | 19 | 9 | 0 | 88 |
| codebase_community | 13 | 34 | 7 | 162 |
| debit_card_specializing | 7 | 0 | 0 | 8 |
| european_football_2 | 41 | 15 | 8 | 455 |
| financial | 10 | 6 | 7 | 109 |
| formula_1 | 19 | 71 | 18 | 287 |
| student_club | 8 | 0 | 0 | 0 |
| superhero | 27 | 2 | 0 | 62 |
| thrombosis_prediction | 1 | 0 | 0 | 0 |
| toxicology | 3 | 2 | 0 | 0 |
| **total** | **150** | **139** | **41** | **1172** |

**No names, frozen key rules** (`out/joins/bird/frozen_no_names`)

| Database | accepted | band | below 60 | gated out |
|---|---|---|---|---|
| california_schools | 2 | 0 | 1 | 1 |
| card_games | 24 | 4 | 0 | 88 |
| codebase_community | 26 | 26 | 2 | 162 |
| debit_card_specializing | 7 | 0 | 0 | 8 |
| european_football_2 | 41 | 15 | 8 | 455 |
| financial | 12 | 9 | 2 | 109 |
| formula_1 | 93 | 14 | 2 | 286 |
| student_club | 8 | 0 | 0 | 0 |
| superhero | 29 | 0 | 0 | 62 |
| thrombosis_prediction | 1 | 0 | 0 | 0 |
| toxicology | 3 | 2 | 0 | 0 |
| **total** | **246** | **70** | **15** | **1171** |

### Checks done

- The whole chain ran from the driver on BIRD dev: driver → filter → Stage 1 → 2a → 2b, all 11 databases. Stages 1 and 2a reproduced the committed files byte for byte.
- 14 unit tests pass.
- Stage 2b also ran without errors on Spider dev (20 databases) and FIBEN (1 database), from the committed Stage 1 and 2a outputs.
- The output feeds the PoC's JoinGR export unchanged (BIRD `financial`: 55 nodes, 23 join edges, 90 intra-table edges), and Stage 3's prompt builder accepts the Stage 1 and 2a files it shares with this stage.
- The stripped and obfuscated BIRD files run as datasets `bird_stripped` and `bird_obfuscated` (2026-10-28). The stripped joins are identical to `out/joins/bird/default`.
- Not run: KaggleDBQA and BEAVER (no Stage 1 or 2a outputs).

---

## 13. Changelog

- **2026-11-01** G9 range-span gate (§4, `min_range_span` 0.1), one unit test (15 pass). Regenerated `out/joins/{bird,beaver,fiben}/default` and `out/joins/{bird,bird_obfuscated}/frozen_no_names`. `out/joins/spider/g9_frozen` is the Spider dev check; the remote's `spider/default`, `spider/no_names` and `bird_stripped/default` are not regenerated.

- **2026-10-24** First version: `src/foreign_keys.py` and `tests/test_foreign_keys.py`, runs on BIRD dev (names and no names).
- **2026-10-31** G8 spares sparse integer primary keys (§4, `pk_source_max_density` 0.5); `out/joins/{bird,beaver,fiben}/default` and `out/joins/{bird,bird_obfuscated}/frozen_no_names` regenerated; one unit test added (14 pass). Not regenerated, so still strict G8: `out/joins/bird_stripped/default`, `out/joins/spider/default` and `out/joins/spider/no_names` (2026-10-28). Until they are rerun, `bird_stripped/default` differs from `bird/default` in the key → key edges only.
