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
| 2 | **G8:** the source is its table's chosen primary key (no PK → PK) | `G8` | [Paper], [Report] |
| 3 | **G1:** the target was hard-rejected by Stage 2a (empty table, nulls, mostly empty/zero, or a blacklisted name when names are used). Uniqueness is already guaranteed, because Stage 1 only makes pairs for fully unique targets. The target does *not* have to be the chosen key | `G1` | [Paper] |
| 4 | **G3:** the target column is named `rowguid` (only when names are used) | `G3` | [Paper] |
| 5 | Source role is boolean, free text or float | `prefilter: source role` | [Paper] tier 1 |
| 6 | Source has fewer than 2 distinct values | `prefilter: fewer than 2` | [Ours] |
| 7 | Source average length is over 64 characters | `prefilter: average length` | [Paper] tier 2, [Team] |
| 8 | **G6:** containment is below 0.75 (`--containment-min`) | `G6` | [Paper], [Report] |

Strict G8 means a column that is *both* its table's primary key and a real foreign key (a 1:1 extension table such as BIRD `frpm.CDSCode → schools.CDSCode`) cannot be found. This is intended and is a known limit (§10).

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

---

## 10. Known limits

- **Strict G8:** a primary key that is also a foreign key (1:1 extension tables) is not found.
- **Containment is not enough on its own.** Small integer columns overlap by chance with a large id range; the gates, the name and cardinality terms and the fan-out penalty are what keep these down. Expect statistics alone to be noisy, and many borderline records to reach the band.
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

python -m pytest tests/test_foreign_keys.py
```

Flags: `--databases`, `--run`, `--keys-run`, `--accept`, `--delta`, `--containment-min`, `--max-targets`, `--self-refs`, `--g4-ratio` / `--g4-multiplier`, `--reprofile`. The names setting always comes from the keys run.

The first command needs the DuckDB file from the shared Hugging Face repo (`src/duckdb_driver.py` downloads it); set `HF_HUB_OFFLINE=1` afterwards to use the cached copy.

---

## 12. What it produced on BIRD dev (not graded)

Counts only. Gold labels are used for evaluation after Stage 3, never here. 1,502 candidates in total; the adaptive shift fired in no database.

**With names** (`out/joins/bird/default`)

| Database | accepted | band | below 60 | gated out |
|---|---|---|---|---|
| california_schools | 1 | 0 | 1 | 2 |
| card_games | 14 | 30 | 12 | 60 |
| codebase_community | 21 | 49 | 22 | 124 |
| debit_card_specializing | 7 | 0 | 0 | 8 |
| european_football_2 | 38 | 270 | 13 | 198 |
| financial | 7 | 16 | 17 | 92 |
| formula_1 | 18 | 90 | 21 | 266 |
| student_club | 8 | 0 | 0 | 0 |
| superhero | 27 | 2 | 0 | 62 |
| thrombosis_prediction | 1 | 0 | 0 | 0 |
| toxicology | 3 | 2 | 0 | 0 |
| **total** | **145** | **459** | **86** | **812** |

**No names, frozen key rules** (`out/joins/bird/frozen_no_names`)

| Database | accepted | band | below 60 | gated out |
|---|---|---|---|---|
| california_schools | 0 | 0 | 0 | 4 |
| card_games | 37 | 19 | 0 | 60 |
| codebase_community | 52 | 40 | 0 | 124 |
| debit_card_specializing | 7 | 0 | 0 | 8 |
| european_football_2 | 302 | 13 | 6 | 198 |
| financial | 28 | 11 | 1 | 92 |
| formula_1 | 114 | 12 | 0 | 269 |
| student_club | 8 | 0 | 0 | 0 |
| superhero | 29 | 0 | 0 | 62 |
| thrombosis_prediction | 1 | 0 | 0 | 0 |
| toxicology | 3 | 2 | 0 | 0 |
| **total** | **581** | **97** | **7** | **817** |

### Checks done

- The whole chain ran from the driver on BIRD dev: driver → filter → Stage 1 → 2a → 2b, all 11 databases. Stages 1 and 2a reproduced the committed files byte for byte.
- 13 unit tests pass.
- Stage 2b also ran without errors on Spider dev (20 databases) and FIBEN (1 database), from the committed Stage 1 and 2a outputs.
- The output feeds the PoC's JoinGR export unchanged (BIRD `financial`: 55 nodes, 23 join edges, 90 intra-table edges), and Stage 3's prompt builder accepts the Stage 1 and 2a files it shares with this stage.
- Not run: the stripped and obfuscated DuckDB files (the output paths have no setting folder yet), KaggleDBQA and BEAVER (no Stage 1 or 2a outputs), Stage 3 with the joins.

---

## 13. Changelog

- **2026-10-24** First version: `src/foreign_keys.py` and `tests/test_foreign_keys.py`, runs on BIRD dev (names and no names).
