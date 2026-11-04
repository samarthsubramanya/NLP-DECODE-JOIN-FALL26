# Stage 2b: foreign keys and the join graph — summary and handover

This covers what Stage 2b does, how to run it, and what is still open. **The full reference (every rule, its source paper, output fields, worked example, results) is [foreign_keys.md](foreign_keys.md).** For Stages 1 and 2a see [stage1_stage2a_summary.md](stage1_stage2a_summary.md); for Stage 3 see [stage3_summary.md](stage3_summary.md).

---

## 1. What this part does

Stage 2b scores every foreign-key candidate with DBAutoDoc's scoring, on **whole columns** (exact containment, no 500-value sample), and builds the join graph. **No LLM** and no gold labels: it reads only `out/profiles` and `out/keys`, never a database, declared keys, `schema_mapping.csv` or `*_metadata`.

The LLM that judges the ambiguous records is **Stage 3's** job. Stage 2b only labels which records need it.

```
out/profiles/<dataset>/<db_id>.json        (Stage 1: stats + containment pairs)
out/keys/<dataset>/<keys-run>/<db_id>.json (Stage 2a: primary keys, eligible columns, scores)
        │
        ▼
src/foreign_keys.py    Stage 2b   ──►  out/joins/<dataset>/<run>/<db_id>.json
```

---

## 2. The rules (`src/foreign_keys.py`, every number is in `DEFAULTS`)

Candidates are the Stage 1 containment pairs, source `a` → target `b`, `I(a→b) = |Va ∩ Vb| / |Va|`.

**Gates and prefilters** (first one that fires rejects the pair as `rejected_gate`, with the reason kept)

| Rule | Meaning |
|---|---|
| same table | excluded unless `--self-refs` |
| G8 | a table's chosen primary key is not a source, unless it is a **sparse integer key**: density (distinct ÷ (max − min + 1)) ≤ 0.5 (`pk_source_max_density`). Subclass tables reuse their parent's key with scattered values from one shared ID pool; keys numbered 1..n overlap other 1..n keys by chance, so they stay gated |
| G1 | the target must have survived 2a's hard rejections. It need not be the chosen key, so `k` can be 0 |
| G3 | a column named `rowguid` is never a target (names only) |
| prefilters | source role boolean / free text / float; fewer than 2 distinct values; average length over 64 |
| G6 | containment ≥ 0.75 |
| G9 | optional hard gate, **off by default** (`min_range_span` is `None`; `--min-range-span 0.1` turns it on): an integer source's range must span that share of the target's. The distribution term below replaced it as the default |

**Score** (points out of 100): `25·v + 15·d + 20·s + 15·r + 15·k + 10·ν`

| Term | Definition |
|---|---|
| `v` | exact containment |
| `d` | distribution (Stage 1 `dist`): `1 − EMD/EMD_max` of where the source's matching values fall over up to 10 quantile buckets of the target's sorted values. 1 = spread over the whole range; a small-integer measure in a corner of an id range scores about 0. Null (the target has fewer than 5 distinct values, the source has no hit, or the profile is old) = no evidence: that record's `d` points go to `v`. It only lowers the score, no penalty or gate |
| `s` | name similarity: best of source name vs `b`, `{b_table}_{b}`, `{b_table}_id`; 1.0 equal, 0.8 one contains the other, else `1 − Levenshtein / longest` |
| `r` | `min(ρ, 2) / 2`, `ρ = rows(source table) / distinct(source column)` |
| `k` | 1 if `b` is its table's chosen primary key, else 0 |
| `ν` | source null share: < 30% → 1.0, < 70% → 0.5, else 0 |

- **No names (obfuscated):** `40·v + 20·d + 15·r + 15·k + 10·ν` (the old 60 overlap points split into containment and distribution).
- **Adaptive:** if more than 40% of the candidates that passed the gates have `k = 0`, the 15 points of `k` go to overlap (25 → 40 named, 40 → 55 without names). Decided per database.
- **Penalties:** × 0.7 when the orphan rate `1 − v` is over 0.2; × 0.5 for incompatible types (inert: Stage 1 pairs are always the same family).
- **Low-distribution cap:** if `d` < `dist_no_accept_below` (0.3), the final score (after penalties, G5 and fan-out) is capped at `accept + delta − 1` (69), so the pair goes to the LLM band and is never auto-accepted. A lower score is not raised; `d` null (no evidence) is not capped; `dist_capped: true` marks a capped candidate. 0.3 is the 5th percentile of `d` under a uniform random-spread null model with 3 source values (about 0.44 at 5, 0.77 at 30); it is not fit to any dataset. `--dist-no-accept-below none` switches it off. It is blunt for sources with 1–2 matching values, where a genuine foreign key can be capped into the band.
- **G4** (source table far smaller than the target) has no multiplier in the paper, so it is a **knob that is off** (`--g4-ratio`, `--g4-multiplier`).
- **G5:** each source keeps its 3 best targets. **ψ(n)** then scales the score by 1.0 / 0.85 / 0.75 / 0.65 for 1 / 2 / 3 kept targets (n counts targets kept after G5).

**Decision** on the final score: below 60 → `rejected_score`; 60 to below 70 → `band` (Stage 3's LLM accepts, rejects or abstains); 70 and above → `accepted`.

**Graph:** greedy, highest score first, one edge per column pair (undirected), at most 3 edges per source, cycles kept.

| Graph | Contains |
|---|---|
| `graphs.direct` | accepted records only: the base the LLM's accepts are added to |
| `graphs.stats` | accepted + band: what statistics alone export (RQ1, variant 2) |
| `graphs.llm` | written by Stage 3: accepted + `llm_accept` |

---

## 3. Output (`out/joins/<dataset>/<run>/<db_id>.json`)

Same shape the PoC's export, describe and eval code read (`candidates`, `graphs`), plus tracing fields.

- Top level: `db_id`, `use_names`, `keys_run`, `weights` (the weights actually used), `adaptive`, `k_zero_share`, `config`, `counts`.
- Each candidate: `source`, `target`, `target_is_pk`, **`target_pk_score`** (the 2a score, for the LLM prompt), `containment`, `coverage`, `cardinality`, `parts` (the five terms), `penalties`, `fan_out`, `score`, `decision`, and `reason` when gated out.

The names setting is inherited from the keys run: a keys run made with `--no-names` gives the no-names scoring. (Do not reuse the committed `out/keys/bird/no_names` run for this: it is the old baseline without the key rules, kept for reference.)

---

## 4. How to run it

```bash
python src/primary_keys.py --dataset bird --run frozen        # Stage 2a, if out/keys/bird/frozen is missing
python src/foreign_keys.py --dataset bird                      # Stage 2b
python src/foreign_keys.py --dataset bird --databases financial --run try
python src/primary_keys.py --dataset bird --run frozen_no_names --no-names                       # 2a with names off
python src/foreign_keys.py --dataset bird --keys-run frozen_no_names --run frozen_no_names     # obfuscated simulation
python -m pytest tests/test_foreign_keys.py                   # unit tests on hand-made profiles
```

Flags: `--accept`, `--delta`, `--containment-min`, `--max-targets`, `--self-refs`, `--pk-source-max-density` (negative = strict G8), `--min-range-span` (G9, off unless given), `--dist-no-accept-below` (cap, default 0.3, `none` = off), `--g4-ratio` / `--g4-multiplier`, `--keys-run`, `--reprofile`.

---

## 5. What it produces on BIRD dev (not graded)

Counts only; gold labels are used for evaluation after Stage 3, never here.

| Run | Candidates | accepted | band | below 60 | gated out |
|---|---|---|---|---|---|
| names (`frozen` keys) | 1,502 | 150 | 139 | 41 | 1,172 |
| no names (`frozen_no_names` keys) | 1,502 | 246 | 70 | 15 | 1,171 |

(Strict G8, before 2026-10-31: 145 / 459 / 86 / 812 with names, 581 / 97 / 7 / 817 without.)

Two things to know before Stage 3:
- **LLM load:** with names the band is 467 records on BIRD dev (276 of them in `european_football_2`).
- **No names:** before the distribution term the 60-point overlap weight pushed most candidates over 70, so far more were accepted outright and the band nearly emptied. The counts above are from that scoring; the distribution term (2026-11-04) has not been measured yet.

---

## 6. Open items

- Email/URL prefilter (paper tier 2) is not applied: it needs a full-column share that Stage 1 does not compute.
- G4 is off until someone chooses a multiplier.
- The setting is the dataset name: `bird` (documented file), `bird_stripped`, `bird_obfuscated`, each with its own `out/filtered/`, `out/profiles/`, `out/keys/`, `out/joins/` and `out/descriptions/` folder. Grading always reads labels from the documented file.
- The G8 density threshold (0.5) was chosen after looking at FIBEN and BIRD; Spider dev and BEAVER confirm it does no harm, but it has not been tested on a second class-hierarchy schema. Non-integer keys keep strict G8 (untested).

## 7. Changelog

- **2026-11-04** Distribution term: Stage 1 stores a quantile-EMD `dist` and `dist_buckets` on every containment pair; Stage 2b adds it to the score (weights above; no evidence folds into overlap per record). It replaces the G9 hard gate as the default (`min_range_span` is now `None`; gate and flag kept for ablation). A record with `d` < 0.3 is capped at 69 (always in the LLM band, never auto-accepted; `dist_no_accept_below`). G8 is intentionally unchanged. Profiles must be rebuilt to carry `dist`; nothing has been regenerated or measured yet.
- **2026-11-01** G9 range-span gate (`min_range_span` 0.1): an integer source must span at least a tenth of the target's range, which removes small measures (ratings, counts) contained by chance in 1..n ids. Regenerated `out/joins/{bird,beaver,fiben}/default` and `out/joins/{bird,bird_obfuscated}/frozen_no_names`. Joins F1 after Stage 3: BIRD obfuscated .275 → .563, BIRD named .733 → .758, FIBEN .306 → .327 (.629 → .701 on FKs with rows). Unit test added (15 pass).
- **2026-10-31** G8 spares sparse integer keys (`pk_source_max_density`, 0.5): finds subclass links (FIBEN: a child table's key references its parent's key; 135 of FIBEN's 159 declared FKs) and 1:1 extension tables (BIRD `frpm.CDSCode → schools.CDSCode`). Regenerated `out/joins/{bird,beaver,fiben,spider}/default` and `out/joins/{bird,bird_obfuscated}/frozen_no_names`. Joins graph F1 after Stage 3 (strict G8 → density rule): FIBEN vs its 159 declared FKs .196 → .306, BIRD named .737 → .733, BIRD obfuscated .271 → .275, BEAVER unchanged; Spider dev, statistics only, .824 → .833. Unit test added (14 pass).
- **2026-10-24** First version of Stage 2b (`src/foreign_keys.py`, `tests/test_foreign_keys.py`, `docs/foreign_keys.md`). Ran the whole chain on BIRD dev; outputs in `out/joins/bird/default` and `out/joins/bird/frozen_no_names`.
