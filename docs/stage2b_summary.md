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
| G8 | a table's chosen primary key is never a source (strict: no PK → PK) |
| G1 | the target must have survived 2a's hard rejections. It need not be the chosen key, so `k` can be 0 |
| G3 | a column named `rowguid` is never a target (names only) |
| prefilters | source role boolean / free text / float; fewer than 2 distinct values; average length over 64 |
| G6 | containment ≥ 0.75 |

**Score** (points out of 100): `40·v + 20·s + 15·r + 15·k + 10·ν`

| Term | Definition |
|---|---|
| `v` | exact containment |
| `s` | name similarity: best of source name vs `b`, `{b_table}_{b}`, `{b_table}_id`; 1.0 equal, 0.8 one contains the other, else `1 − Levenshtein / longest` |
| `r` | `min(ρ, 2) / 2`, `ρ = rows(source table) / distinct(source column)` |
| `k` | 1 if `b` is its table's chosen primary key, else 0 |
| `ν` | source null share: < 30% → 1.0, < 70% → 0.5, else 0 |

- **No names (obfuscated):** the 20 name points go to overlap → `60·v + 15·r + 15·k + 10·ν`.
- **Adaptive:** if more than 40% of the candidates that passed the gates have `k = 0`, the 15 points of `k` go to overlap. Decided per database.
- **Penalties:** × 0.7 when the orphan rate `1 − v` is over 0.2; × 0.5 for incompatible types (inert: Stage 1 pairs are always the same family).
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

Flags: `--accept`, `--delta`, `--containment-min`, `--max-targets`, `--self-refs`, `--g4-ratio` / `--g4-multiplier`, `--keys-run`, `--reprofile`.

---

## 5. What it produces on BIRD dev (not graded)

Counts only; gold labels are used for evaluation after Stage 3, never here.

| Run | Candidates | accepted | band | below 60 | gated out |
|---|---|---|---|---|---|
| names (`frozen` keys) | 1,502 | 145 | 459 | 86 | 812 |
| no names (`frozen_no_names` keys) | 1,502 | 581 | 97 | 7 | 817 |

Two things to know before Stage 3:
- **LLM load:** with names the band is 459 records on BIRD dev (270 of them in `european_football_2`).
- **No names:** the 60-point overlap weight pushes most candidates over 70, so far more are accepted outright and the band nearly empties. This is the scoring as specified; it is a result to report, not something tuned here.

---

## 6. Open items

- Email/URL prefilter (paper tier 2) is not applied: it needs a full-column share that Stage 1 does not compute.
- G4 is off until someone chooses a multiplier.
- Output paths have no setting folder yet; running stripped or obfuscated databases needs one (also for Stages 1, 2a and 3).
- Stage 3 still builds its own overlap hints; it should read `out/joins` and run the band adjudication.

## 7. Changelog

- **2026-10-24** First version of Stage 2b (`src/foreign_keys.py`, `tests/test_foreign_keys.py`, `docs/foreign_keys.md`). Ran the whole chain on BIRD dev; outputs in `out/joins/bird/default` and `out/joins/bird/frozen_no_names`.
